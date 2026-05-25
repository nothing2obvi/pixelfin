import json
import os
from datetime import datetime
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import requests

from generate_html import (
	IMAGE_TYPES_MAP,
	check_low_res,
	extract_year,
	find_image_tags,
	get_first_user_id,
	get_library_id,
	get_library_items,
	get_image_resolution,
	get_season_primary_image_url,
	get_series_seasons,
	_parse_season_number,
)


IMAGE_TYPE_OPTIONS = {
	"p": "Primary",
	"c": "Art",
	"bd": "Backdrop",
	"bn": "Banner",
	"b": "Box",
	"br": "BoxRear",
	"d": "Disc",
	"l": "Logo",
	"m": "Menu",
	"t": "Thumb",
	"sp": "Season Posters",
}

DEFAULT_SELECTED_IMAGES = ["p", "t", "l", "bd"]
DEFAULT_THRESHOLDS = {
	"p": [680, 1000],
	"bd": [1920, 1080],
}
DEFAULT_HIGH_THRESHOLDS = {
	"p": [2000, 3000],
	"bd": [3840, 2160],
}
DEFAULT_ZIP_BASENAMES = {
	"p": "cover",
	"t": "thumbnail",
	"bd": "backdrop",
	"c": "art",
	"bn": "banner",
	"b": "box",
	"br": "boxrear",
	"d": "disc",
	"l": "logo",
	"m": "menu",
	"sp": "season-poster",
}

UNSUPPORTED_LIBRARY_TYPES = {"livetv", "playlists", "playlist"}
UNSUPPORTED_LIBRARY_NAMES = {"live tv", "livetv", "playlists", "playlist"}


def _norm_library_kind(value):
	return str(value or "").strip().lower().replace("-", "").replace("_", "").replace(" ", "")


def is_supported_library(view_or_row):
	collection_type = _norm_library_kind((view_or_row or {}).get("CollectionType") or (view_or_row or {}).get("collection_type"))
	name = _norm_library_kind((view_or_row or {}).get("Name") or (view_or_row or {}).get("name"))
	if collection_type in UNSUPPORTED_LIBRARY_TYPES:
		return False
	if name in UNSUPPORTED_LIBRARY_NAMES:
		return False
	return True


def add_jellytag_bypass(url, enabled):
	if not enabled:
		return url
	parts = urlsplit(url)
	query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "jellytag"]
	query.append(("jellytag", "off"))
	return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def jellyfin_headers(api_key):
	return {"X-Emby-Token": api_key, "User-Agent": "Pixelfin-Fresh"}


def test_server(server):
	resp = requests.get(
		f"{server['url'].rstrip('/')}/System/Info/Public",
		headers=jellyfin_headers(server["api_key"]),
		timeout=(5, 15),
	)
	resp.raise_for_status()
	return resp.json()


def list_admin_users(server):
	resp = requests.get(
		f"{server['url'].rstrip('/')}/Users",
		headers=jellyfin_headers(server["api_key"]),
		timeout=(5, 15),
	)
	resp.raise_for_status()
	users = []
	for user in resp.json() or []:
		policy = user.get("Policy") or {}
		if not policy.get("IsAdministrator") or policy.get("IsDisabled", False):
			continue
		users.append(
			{
				"id": user.get("Id") or "",
				"name": user.get("Name") or user.get("Id") or "Admin user",
			}
		)
	return sorted([user for user in users if user["id"]], key=lambda user: user["name"].lower())


def _server_user_id(server):
	return (server or {}).get("sync_user_id") or get_first_user_id(server["url"], server["api_key"])


def list_views(server):
	user_id = _server_user_id(server)
	resp = requests.get(
		f"{server['url'].rstrip('/')}/Users/{user_id}/Views",
		headers=jellyfin_headers(server["api_key"]),
		timeout=(5, 30),
	)
	resp.raise_for_status()
	views = (resp.json() or {}).get("Items", []) or []
	result = []
	for view in views:
		view_id = view.get("Id")
		if not view_id:
			continue
		if not is_supported_library(view):
			continue
		result.append(
			{
				"id": view_id,
				"name": view.get("Name") or "Untitled",
				"collection_type": view.get("CollectionType") or "",
				"thumbnail_url": library_thumbnail_url(server, view),
			}
		)
	return result


def library_thumbnail_url(server, view):
	view_id = view.get("Id")
	tag = (view.get("ImageTags") or {}).get("Primary")
	if tag:
		return f"{server['url'].rstrip('/')}/Items/{view_id}/Images/Primary?tag={tag}&api_key={server['api_key']}"
	return f"{server['url'].rstrip('/')}/Items/{view_id}/Images/Primary?api_key={server['api_key']}"


def item_details_url(server, item_id):
	return f"{server['url'].rstrip('/')}/web/index.html#!/details?id={item_id}"


def normalize_thresholds(thresholds):
	norm = {}
	for code, value in (thresholds or {}).items():
		try:
			if code in IMAGE_TYPE_OPTIONS and len(value) == 2:
				w = int(value[0])
				h = int(value[1])
				if w > 0 and h > 0:
					norm[code] = [w, h]
		except Exception:
			continue
	return norm


def check_high_res(code, width, height, maxres):
	if code not in maxres:
		return False
	try:
		max_w, max_h = maxres[code]
		return int(width or 0) > int(max_w) or int(height or 0) > int(max_h)
	except Exception:
		return False


def _season_poster_label(season):
	season_num = _parse_season_number(season)
	if season_num == 0:
		return "specials-poster"
	if season_num is not None:
		return f"season{season_num:02d}-poster"
	return str(season.get("Name") or "season-poster").strip() or "season-poster"


def _image_row(code, label, url, width, height, minres, maxres, high_enabled=False):
	is_placeholder = int(width or 0) == 1 and int(height or 0) == 1
	is_missing = bool(is_placeholder or not int(width or 0))
	is_low = bool(check_low_res(code, width, height, minres)) and not is_missing
	is_high = bool(high_enabled and check_high_res(code, width, height, maxres)) and not is_missing
	status = "missing" if is_missing else ("low" if is_low else ("high" if is_high else "ok"))
	return (
		code,
		label,
		url,
		int(width or 0),
		int(height or 0),
		status,
		int(is_low),
		int(is_missing),
		int(is_high),
	)


def _row_needs_attention(row):
	return bool(row[6] or row[7] or row[8])


def _season_poster_rows(item, server, user_id, minres, maxres=None, high_enabled=False, jellytag_bypass=False):
	if (item.get("Type") or "").lower() != "series":
		return []
	rows = []
	try:
		seasons = get_series_seasons(server["url"], server["api_key"], user_id, item.get("Id"))
	except Exception:
		seasons = []
	for season in seasons:
		label = _season_poster_label(season)
		if not ((season.get("ImageTags") or {}).get("Primary")):
			rows.append(_image_row("sp", label, "", 0, 0, minres, maxres or {}, high_enabled))
			continue
		url = get_season_primary_image_url(season, server["url"], server["api_key"], jellytag_bypass=jellytag_bypass)
		if not url:
			rows.append(_image_row("sp", label, "", 0, 0, minres, maxres or {}, high_enabled))
			continue
		width, height = get_image_resolution(url)
		rows.append(_image_row("sp", label, url, width, height, minres, maxres or {}, high_enabled))
	return rows


def scan_library(conn, server, library_row, global_thresholds=None, global_high_thresholds=None, criteria=None, jellytag_bypass=False):
	global_thresholds = normalize_thresholds(global_thresholds or {})
	selected_images = json.loads(library_row["selected_images"] or "[]") or list(DEFAULT_SELECTED_IMAGES)
	scan_images = list(IMAGE_TYPES_MAP.keys())

	thresholds = dict(global_thresholds)
	thresholds.update(normalize_thresholds(json.loads(library_row["thresholds"] or "{}")))
	minres = {code: tuple(value) for code, value in thresholds.items()}
	high_thresholds = dict(normalize_thresholds(global_high_thresholds or {}))
	high_thresholds.update(normalize_thresholds(json.loads(library_row["high_thresholds"] or "{}")))
	maxres = {code: tuple(value) for code, value in high_thresholds.items()}
	criteria = criteria or {}
	high_enabled = bool(criteria.get("high_resolution"))

	user_id = _server_user_id(server)
	library_id, library_type = get_library_id(server["url"], server["api_key"], user_id, library_row["name"])
	if not library_id:
		raise RuntimeError(f"Library '{library_row['name']}' not found")

	items = get_library_items(server["url"], server["api_key"], user_id, library_id, library_type)
	full_items = []
	session = requests.Session()
	session.headers.update(jellyfin_headers(server["api_key"]))

	for item in items:
		item_id = item.get("Id")
		if not item_id:
			continue
		try:
			resp = session.get(f"{server['url'].rstrip('/')}/Users/{user_id}/Items/{item_id}", timeout=(5, 20))
			resp.raise_for_status()
			full = resp.json()
			full["Id"] = item_id
		except Exception:
			full = dict(item)
		full_items.append(full)

	now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
	task_count = 0
	conn.execute("DELETE FROM item_images WHERE server_id = ? AND item_id IN (SELECT id FROM media_items WHERE server_id = ? AND library_id = ?)", (server["id"], server["id"], library_row["id"]))
	conn.execute("DELETE FROM media_items WHERE server_id = ? AND library_id = ?", (server["id"], library_row["id"]))

	for item in full_items:
		item_id = item.get("Id")
		if not item_id:
			continue

		image_rows = []
		for row in _season_poster_rows(item, server, user_id, minres, maxres, high_enabled, jellytag_bypass=jellytag_bypass):
			image_rows.append(row)
		for code in scan_images:
			image_type = IMAGE_TYPES_MAP.get(code)
			if not image_type:
				continue
			tags = find_image_tags(
				item,
				image_type,
				server["url"],
				server["api_key"],
				jellytag_bypass=jellytag_bypass,
			)
			if not tags:
				image_rows.append(_image_row(code, image_type, "", 0, 0, minres, maxres, high_enabled))
				continue
			for label, url, width, height in tags:
				url = add_jellytag_bypass(url, jellytag_bypass)
				row = _image_row(code, label, url, width, height, minres, maxres, high_enabled)
				image_rows.append(row)
		needs_attention = any(row[0] in selected_images and _row_needs_attention(row) for row in image_rows)

		if needs_attention:
			task_count += 1

		conn.execute(
			"""
			INSERT INTO media_items(server_id, id, library_id, name, year, item_type, sort_name, date_added, needs_attention, details_url, raw_json, last_scanned)
			VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			""",
			(
				server["id"],
				item_id,
				library_row["id"],
				item.get("Name") or "Untitled",
				extract_year(item) or "",
				item.get("Type") or "",
				item.get("SortName") or item.get("Name") or "",
				item.get("DateCreated") or item.get("DateAdded") or "",
				int(needs_attention),
				item_details_url(server, item_id),
				json.dumps(item),
				now,
			),
		)
		for code, label, url, width, height, status, is_low, is_missing, is_high in image_rows:
			conn.execute(
				"""
				INSERT INTO item_images(server_id, item_id, code, label, url, width, height, status, is_low, is_missing, is_high, last_checked)
				VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
				""",
				(server["id"], item_id, code, label, url, width, height, status, is_low, is_missing, is_high, now),
			)

	conn.execute(
		"UPDATE libraries SET collection_type = ?, item_count = ?, task_count = ?, last_scanned = ? WHERE server_id = ? AND id = ?",
		(library_type or library_row["collection_type"] or "", len(full_items), task_count, now, server["id"], library_row["id"]),
	)
	conn.commit()
	return {"items": len(full_items), "tasks": task_count, "last_scanned": now}


def scan_media_item(conn, server, library_row, item_id, global_thresholds=None, global_high_thresholds=None, criteria=None, jellytag_bypass=False):
	global_thresholds = normalize_thresholds(global_thresholds or {})
	selected_images = json.loads(library_row["selected_images"] or "[]") or list(DEFAULT_SELECTED_IMAGES)
	scan_images = list(IMAGE_TYPES_MAP.keys())

	thresholds = dict(global_thresholds)
	thresholds.update(normalize_thresholds(json.loads(library_row["thresholds"] or "{}")))
	minres = {code: tuple(value) for code, value in thresholds.items()}
	high_thresholds = dict(normalize_thresholds(global_high_thresholds or {}))
	high_thresholds.update(normalize_thresholds(json.loads(library_row["high_thresholds"] or "{}")))
	maxres = {code: tuple(value) for code, value in high_thresholds.items()}
	criteria = criteria or {}
	high_enabled = bool(criteria.get("high_resolution"))

	user_id = _server_user_id(server)
	session = requests.Session()
	session.headers.update(jellyfin_headers(server["api_key"]))
	resp = session.get(f"{server['url'].rstrip('/')}/Users/{user_id}/Items/{item_id}", timeout=(5, 20))
	resp.raise_for_status()
	item = resp.json()
	item["Id"] = item_id

	now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
	image_rows = []
	for row in _season_poster_rows(item, server, user_id, minres, maxres, high_enabled, jellytag_bypass=jellytag_bypass):
		image_rows.append(row)
	for code in scan_images:
		image_type = IMAGE_TYPES_MAP.get(code)
		if not image_type:
			continue
		tags = find_image_tags(
			item,
			image_type,
			server["url"],
			server["api_key"],
			jellytag_bypass=jellytag_bypass,
		)
		if not tags:
			image_rows.append(_image_row(code, image_type, "", 0, 0, minres, maxres, high_enabled))
			continue
		for label, url, width, height in tags:
			url = add_jellytag_bypass(url, jellytag_bypass)
			row = _image_row(code, label, url, width, height, minres, maxres, high_enabled)
			image_rows.append(row)
	needs_attention = any(row[0] in selected_images and _row_needs_attention(row) for row in image_rows)

	conn.execute("DELETE FROM item_images WHERE server_id = ? AND item_id = ?", (server["id"], item_id))
	conn.execute(
		"""
		INSERT INTO media_items(server_id, id, library_id, name, year, item_type, sort_name, date_added, needs_attention, details_url, raw_json, last_scanned)
		VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(server_id, id) DO UPDATE SET
			name = excluded.name,
			year = excluded.year,
			item_type = excluded.item_type,
			sort_name = excluded.sort_name,
			date_added = excluded.date_added,
			needs_attention = excluded.needs_attention,
			details_url = excluded.details_url,
			raw_json = excluded.raw_json,
			last_scanned = excluded.last_scanned
		""",
		(
			server["id"],
			item_id,
			library_row["id"],
			item.get("Name") or "Untitled",
			extract_year(item) or "",
			item.get("Type") or "",
			item.get("SortName") or item.get("Name") or "",
			item.get("DateCreated") or item.get("DateAdded") or "",
			int(needs_attention),
			item_details_url(server, item_id),
			json.dumps(item),
			now,
		),
	)
	for code, label, url, width, height, status, is_low, is_missing, is_high in image_rows:
		conn.execute(
			"""
			INSERT INTO item_images(server_id, item_id, code, label, url, width, height, status, is_low, is_missing, is_high, last_checked)
			VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			""",
			(server["id"], item_id, code, label, url, width, height, status, is_low, is_missing, is_high, now),
		)
	task_count = conn.execute(
		"SELECT COUNT(*) FROM media_items WHERE server_id = ? AND library_id = ? AND needs_attention = 1",
		(server["id"], library_row["id"]),
	).fetchone()[0]
	conn.execute(
		"UPDATE libraries SET task_count = ?, last_scanned = ? WHERE server_id = ? AND id = ?",
		(task_count, now, server["id"], library_row["id"]),
	)
	conn.commit()
	return {"item": item_id, "needs_attention": bool(needs_attention), "tasks": task_count, "last_scanned": now}
