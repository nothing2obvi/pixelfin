#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
restore.py — Pixelfin Restore Engine
v0.2.2

Changes:
- preserves existing restore behavior
- keeps forced_mappings support
- adds stronger Jellyfin library enumeration strategy
- if ParentId-scoped fetches look capped, performs global user-wide fetch
  and filters back down to the selected library using filesystem paths
"""

from __future__ import annotations

import os
import sys
import json
import zipfile
import shutil
import tempfile
import requests
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from difflib import SequenceMatcher
import re

# ---------------------------------------------------------------------
# Force working directory to the script's own folder (/app in container)
# ---------------------------------------------------------------------
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ================================================================
# Config / Constants
# ================================================================
_DEFAULT_TIMEOUT: Tuple[int, int] = (10, 30)
USER_AGENT = "Pixelfin-Restore/0.2.2"
SESSION = requests.Session()

# If best_score < this floor, we treat as truly "unmatched" (no plausible library match).
# You can override via env PIXELFIN_UNMATCHED_FLOOR (0..1)
_DEFAULT_UNMATCHED_FLOOR = 0.45


# =============================================================================
# Utility helpers
# =============================================================================
def _match_components(a: str, b: str) -> Tuple[float, float, float]:
	"""
	Return (score, jaccard_word_overlap, char_similarity).
	Score is 0..1.
	"""
	if not a or not b:
		return 0.0, 0.0, 0.0

	a0 = a.lower().strip()
	b0 = b.lower().strip()

	words_a = set(re.findall(r"[a-z0-9]+", a0))
	words_b = set(re.findall(r"[a-z0-9]+", b0))

	intersection = len(words_a & words_b)
	union = len(words_a | words_b) or 1
	jaccard = intersection / union

	char_sim = SequenceMatcher(None, a0, b0).ratio()

	penalty = 0.0
	if jaccard < 0.25 and char_sim > 0.6:
		penalty = (0.6 - jaccard) * 0.3

	score = (0.7 * jaccard) + (0.3 * char_sim) - penalty
	score = max(0.0, min(score, 1.0))
	return score, jaccard, char_sim


def fuzzy_match(a: str, b: str) -> float:
	return _match_components(a, b)[0]


def ensure_dir(path: str) -> None:
	os.makedirs(path, exist_ok=True)


def safe_basename(name: str) -> str:
	return (name or "").replace("/", "_").replace("\\", "_").strip()


def log(msg: str) -> None:
	print(msg)
	sys.stdout.flush()


def _normalize_title(s: str) -> str:
	return re.sub(r"\s+", " ", (s or "").strip()).casefold()


def _merge_unique_items(*groups: List[Dict]) -> List[Dict]:
	merged: List[Dict] = []
	seen_ids = set()

	for group in groups:
		for item in group or []:
			item_id = (item or {}).get("Id")
			if item_id:
				if item_id in seen_ids:
					continue
				seen_ids.add(item_id)
			merged.append(item)

	return merged


def _looks_suspiciously_capped(items: List[Dict]) -> bool:
	n = len(items or [])
	if n == 0:
		return True
	if n in (100, 200, 300):
		return True
	if 0 < n < 150:
		return True
	return False


def _norm_path(p: str) -> str:
	if not p:
		return ""
	p = p.replace("\\", "/").rstrip("/")
	return p.casefold()


def _path_under_locations(item_path: str, library_locations: List[str]) -> bool:
	ip = _norm_path(item_path)
	if not ip:
		return False

	for loc in library_locations or []:
		lp = _norm_path(loc)
		if not lp:
			continue
		if ip == lp or ip.startswith(lp + "/"):
			return True
	return False


# =============================================================================
# Jellyfin API helpers
# =============================================================================
def _req(method: str, url: str, apikey: str, **kwargs) -> requests.Response:
	headers = kwargs.pop("headers", {})
	headers["X-Emby-Token"] = apikey
	headers["User-Agent"] = USER_AGENT
	r = SESSION.request(method, url, headers=headers, timeout=_DEFAULT_TIMEOUT, **kwargs)
	if not r.ok:
		raise RuntimeError(f"{method} {url} failed {r.status_code}: {r.text[:300]}")
	return r


def _pick_user(server: str, apikey: str) -> str:
	r = SESSION.get(
		f"{server.rstrip('/')}/Users",
		headers={"X-Emby-Token": apikey, "User-Agent": USER_AGENT},
		timeout=_DEFAULT_TIMEOUT,
	)
	r.raise_for_status()
	users = r.json() or []
	user = next((u.get("Id") for u in users if u and not u.get("IsHidden")), None)
	if not user:
		raise RuntimeError("No visible Jellyfin users found")
	return user


def _get_views(server: str, apikey: str, user_id: str) -> List[Dict]:
	r = SESSION.get(
		f"{server.rstrip('/')}/Users/{user_id}/Views",
		headers={"X-Emby-Token": apikey, "User-Agent": USER_AGENT},
		timeout=_DEFAULT_TIMEOUT,
	)
	r.raise_for_status()
	return (r.json() or {}).get("Items", []) or []


def _find_library_view(server: str, apikey: str, user_id: str, library: str) -> Dict:
	views = _get_views(server, apikey, user_id)
	for v in views:
		if (v.get("Name") or "").lower() == (library or "").lower():
			return v
	raise RuntimeError(f"Library '{library}' not found")


def _single_shot_items(
	server: str,
	apikey: str,
	url: str,
	params_base: Dict,
	source_label: str = "single",
) -> List[Dict]:
	params = dict(params_base)
	params["EnableTotalRecordCount"] = "true"
	params["SortBy"] = "SortName"
	params["SortOrder"] = "Ascending"
	params["StartIndex"] = 0
	params["Limit"] = 100000

	try:
		r = _req("GET", url, apikey, params=params)
		data = r.json() or {}
		items = data.get("Items", []) or []
		total = data.get("TotalRecordCount")

		first_id = (items[0] or {}).get("Id") if items else None
		last_id = (items[-1] or {}).get("Id") if items else None
		first_name = (items[0] or {}).get("Name") if items else None
		last_name = (items[-1] or {}).get("Name") if items else None

		log(
			f"[SINGLE:{source_label}] returned={len(items)} total={total} "
			f"first_id={first_id} last_id={last_id} "
			f"first_name={repr(first_name)} last_name={repr(last_name)}"
		)
		return items
	except Exception as e:
		log(f"[WARN] Single-shot fetch failed for {source_label}: {e}")
		return []


def _page_items(
	server: str,
	apikey: str,
	url: str,
	params_base: Dict,
	limit: int = 100,
	max_pages: int = 20000,
	source_label: str = "paged",
) -> List[Dict]:
	all_items: List[Dict] = []
	seen_ids = set()
	seen_page_signatures = set()
	start = 0
	total: Optional[int] = None
	consecutive_no_progress = 0

	for page_num in range(1, max_pages + 1):
		params = dict(params_base)
		params["EnableTotalRecordCount"] = "true"
		params["SortBy"] = "SortName"
		params["SortOrder"] = "Ascending"
		params["StartIndex"] = start
		params["Limit"] = limit

		r = _req("GET", url, apikey, params=params)
		data = r.json() or {}
		chunk = data.get("Items", []) or []

		if total is None and data.get("TotalRecordCount") is not None:
			try:
				total = int(data["TotalRecordCount"])
			except Exception:
				total = None

		if not chunk:
			log(f"[PAGE:{source_label}] page={page_num} start={start} returned=0 total={total} -> stop")
			break

		first_id = (chunk[0] or {}).get("Id")
		last_id = (chunk[-1] or {}).get("Id")
		first_name = (chunk[0] or {}).get("Name")
		last_name = (chunk[-1] or {}).get("Name")

		page_sig = tuple(
			f"{(item or {}).get('Id') or ''}|{(item or {}).get('Name') or ''}"
			for item in chunk
		)

		repeated_page = page_sig in seen_page_signatures
		if not repeated_page:
			seen_page_signatures.add(page_sig)

		added_this_page = 0
		for item in chunk:
			item_id = (item or {}).get("Id")
			if item_id:
				if item_id in seen_ids:
					continue
				seen_ids.add(item_id)
			all_items.append(item)
			added_this_page += 1

		log(
			f"[PAGE:{source_label}] page={page_num} start={start} returned={len(chunk)} "
			f"added={added_this_page} total={total} "
			f"first_id={first_id} last_id={last_id} "
			f"first_name={repr(first_name)} last_name={repr(last_name)}"
		)

		if repeated_page or added_this_page == 0:
			consecutive_no_progress += 1
			log(
				f"[WARN] No pagination progress at start={start} "
				f"(page={page_num}, repeated={repeated_page}, no_progress={consecutive_no_progress})"
			)
		else:
			consecutive_no_progress = 0

		if consecutive_no_progress >= 2:
			log(f"[WARN] Stopping {source_label} pagination after 2 no-progress pages.")
			break

		if len(chunk) < limit:
			log(
				f"[PAGE:{source_label}] short page at start={start} "
				f"(returned={len(chunk)} < limit={limit}) -> stop"
			)
			break

		start += limit

		if total is not None and start >= total:
			log(
				f"[PAGE:{source_label}] reached/passed reported total "
				f"(next_start={start} total={total}) -> stop"
			)
			break

	return all_items


def _global_filtered_by_locations(
	server: str,
	apikey: str,
	user_id: str,
	library_locations: List[str],
	include_item_types: bool = True,
) -> List[Dict]:
	"""
	Global user-wide fetch, then filter back down by filesystem path.
	This is the strongest fallback when ParentId-scoped requests look capped.
	"""
	url_users = f"{server.rstrip('/')}/Users/{user_id}/Items"
	url_root = f"{server.rstrip('/')}/Items"

	params_common = {
		"Recursive": "true",
		"Fields": "PrimaryImageAspectRatio,Type,SortName,Path",
		"EnableTotalRecordCount": "true",
		"SortBy": "SortName",
		"SortOrder": "Ascending",
	}
	if include_item_types:
		params_common["IncludeItemTypes"] = "Movie,Series,MusicVideo,BoxSet"

	params_root = dict(params_common)
	params_root["UserId"] = user_id

	global_users_single = _single_shot_items(
		server, apikey, url_users, params_common, source_label="global-users"
	)
	global_users_paged = _page_items(
		server, apikey, url_users, params_common, limit=100, source_label="global-users"
	)
	global_root_single = _single_shot_items(
		server, apikey, url_root, params_root, source_label="global-root"
	)
	global_root_paged = _page_items(
		server, apikey, url_root, params_root, limit=100, source_label="global-root"
	)

	global_merged = _merge_unique_items(
		global_users_single,
		global_users_paged,
		global_root_single,
		global_root_paged,
	)

	filtered: List[Dict] = []
	seen_ids = set()

	for item in global_merged:
		item_path = (item or {}).get("Path") or ""
		item_id = (item or {}).get("Id")
		if not _path_under_locations(item_path, library_locations):
			continue
		if item_id:
			if item_id in seen_ids:
				continue
			seen_ids.add(item_id)
		filtered.append(item)

	log(
		f"[INFO] Global fetch filtered by library paths -> {len(filtered)} items "
		f"(library_locations={library_locations})"
	)
	return filtered


def get_library_items(server: str, apikey: str, library: str) -> Tuple[List[Dict], str]:
	"""
	Return ALL items in a Jellyfin library by name.

	Strategy:
	1) Try scoped fetches using ParentId on both /Users/{id}/Items and /Items?UserId=...
	2) If result still looks suspiciously capped, do a global user-wide fetch
	   and filter by the selected library's filesystem locations using Path.
	"""
	user_id = _pick_user(server, apikey)
	view = _find_library_view(server, apikey, user_id, library)
	parent_id = view.get("Id")
	collection_type = (view.get("CollectionType") or "").lower()

	library_locations = view.get("Locations") or []
	if not isinstance(library_locations, list):
		library_locations = []

	if not parent_id:
		raise RuntimeError(f"Library '{library}' returned no Id")

	log(f"[INFO] Library view id: {parent_id}")
	log(f"[INFO] Library locations: {library_locations}")

	params_scoped = {
		"ParentId": parent_id,
		"Recursive": "true",
		"IncludeItemTypes": "Movie,Series,MusicVideo,BoxSet",
		"Fields": "PrimaryImageAspectRatio,Type,SortName,Path",
		"EnableTotalRecordCount": "true",
		"SortBy": "SortName",
		"SortOrder": "Ascending",
	}

	url_users = f"{server.rstrip('/')}/Users/{user_id}/Items"
	url_root = f"{server.rstrip('/')}/Items"
	params_root = dict(params_scoped)
	params_root["UserId"] = user_id

	log(f"[INFO] Fetching library '{library}' via scoped endpoints...")

	items_users_single = _single_shot_items(
		server, apikey, url_users, params_scoped, source_label="scoped-users"
	)
	items_users_paged = _page_items(
		server, apikey, url_users, params_scoped, limit=100, source_label="scoped-users"
	)
	items_root_single = _single_shot_items(
		server, apikey, url_root, params_root, source_label="scoped-root"
	)
	items_root_paged = _page_items(
		server, apikey, url_root, params_root, limit=100, source_label="scoped-root"
	)

	scoped_merged = _merge_unique_items(
		items_users_single,
		items_users_paged,
		items_root_single,
		items_root_paged,
	)

	log(
		f"[INFO] Scoped library fetch count for '{library}': {len(scoped_merged)} "
		f"(users-single={len(items_users_single)}, users-paged={len(items_users_paged)}, "
		f"root-single={len(items_root_single)}, root-paged={len(items_root_paged)})"
	)

	global_filtered: List[Dict] = []
	global_filtered_no_types: List[Dict] = []

	if _looks_suspiciously_capped(scoped_merged) and library_locations:
		log("[WARN] Scoped result looks suspiciously capped; trying global path-filtered fallback...")
		global_filtered = _global_filtered_by_locations(
			server=server,
			apikey=apikey,
			user_id=user_id,
			library_locations=library_locations,
			include_item_types=True,
		)

		if _looks_suspiciously_capped(global_filtered):
			log("[WARN] Global path-filtered result still looks suspicious; retrying without IncludeItemTypes...")
			global_filtered_no_types = _global_filtered_by_locations(
				server=server,
				apikey=apikey,
				user_id=user_id,
				library_locations=library_locations,
				include_item_types=False,
			)

	merged_items = _merge_unique_items(
		scoped_merged,
		global_filtered,
		global_filtered_no_types,
	)

	log(
		f"[INFO] Final merged item count for library '{library}': {len(merged_items)} "
		f"(scoped={len(scoped_merged)}, global_filtered={len(global_filtered)}, "
		f"global_filtered_no_types={len(global_filtered_no_types)})"
	)
	return merged_items, collection_type


def delete_images(server: str, apikey: str, item_id: str, image_type: str) -> None:
	url = f"{server.rstrip('/')}/Items/{item_id}/Images/{image_type}"
	try:
		r = SESSION.delete(
			url,
			headers={"X-Emby-Token": apikey, "User-Agent": USER_AGENT},
			timeout=_DEFAULT_TIMEOUT,
		)
		if r.status_code not in (200, 204):
			log(f"[WARN] Delete {image_type} for {item_id} -> {r.status_code}")
	except Exception as e:
		log(f"[ERROR] Delete {image_type}: {e}")


def upload_image(server: str, apikey: str, item_id: str, image_type: str, image_path: str) -> None:
	"""
	Upload image to Jellyfin reliably using Base64-encoded body (legacy-safe).
	"""
	import base64
	import mimetypes
	import time
	from PIL import Image

	url = f"{server.rstrip('/')}/Items/{item_id}/Images/{image_type.title()}"
	mime, _ = mimetypes.guess_type(image_path)
	if not mime:
		mime = "image/jpeg"
	mime = mime.lower()

	try:
		with Image.open(image_path) as im:
			log(f"Uploading {os.path.basename(image_path)} ({im.format}, {im.width}x{im.height}) as {mime}")
	except Exception:
		log(f"Uploading {os.path.basename(image_path)} as {mime}")

	with open(image_path, "rb") as f:
		img_b64 = base64.b64encode(f.read()).decode("ascii")

	headers = {"X-Emby-Token": apikey, "Content-Type": mime, "User-Agent": USER_AGENT}
	delay = 1.5
	max_retries = 10

	for attempt in range(1, max_retries + 1):
		try:
			r = SESSION.post(url, headers=headers, data=img_b64, timeout=_DEFAULT_TIMEOUT)
			if r.status_code in (200, 204):
				log(f"Uploaded {image_type} for {item_id} ({os.path.basename(image_path)}) attempt {attempt}/{max_retries}")
				return
			log(f"Upload failed ({r.status_code}): {r.text[:200]} ({attempt}/{max_retries})")
		except requests.exceptions.RequestException as e:
			log(f"Upload error: {e} ({attempt}/{max_retries})")

		if attempt < max_retries:
			log(f"Retrying in {delay:.1f}s...")
			time.sleep(delay)
			delay = min(delay * 1.5, 30.0)

	log(f"Exhausted {max_retries} upload attempts for {os.path.basename(image_path)}")


# =============================================================================
# Report utilities (HTML)
# =============================================================================
def score_color(score: float) -> str:
	if score >= 0.97:
		return "#4CAF50"
	if score >= 0.90:
		return "#FFC107"
	if score >= 0.80:
		return "#FF9800"
	return "#F44336"


def embed_image(p: str, label: str, css: str = "") -> str:
	if not os.path.exists(p):
		return f"<div style='color:#888;text-align:center;'>[{label} missing]</div>"
	try:
		from PIL import Image
		import base64

		with Image.open(p) as im:
			w, h = im.size
		with open(p, "rb") as f:
			b64 = base64.b64encode(f.read()).decode("ascii")

		mime = "image/png" if p.lower().endswith(".png") else "image/jpeg"

		match = re.match(r"^(Before|After|Would be After)\s*\(([^)]+)\)", label)
		if match:
			status = match.group(1)
			img_type = match.group(2)
		else:
			status, img_type = "Image", label

		resolution = f"({w}x{h})"

		caption_html = (
			"<div class='caption' style='text-align:center;line-height:1.4em;margin-top:6px;color:#ccc;'>"
			f"<div style='font-weight:bold;color:#ffb347;'>[{status}]</div>"
			f"<div style='color:#b19cd9;'>{img_type}</div>"
			f"<div style='color:#999;font-size:0.9em;'>{resolution}</div>"
			"</div>"
		)

		return (
			"<div style='display:flex;flex-direction:column;align-items:center;width:100%;'>"
			f"<img class='{css}' src='data:{mime};base64,{b64}' "
			f"data-caption='[{status}] — {img_type} {resolution}' "
			f"style='max-width:98%;height:auto;border-radius:10px;'>"
			f"{caption_html}</div>"
		)
	except Exception as e:
		return f"<div style='color:#888;text-align:center;'>[error loading {label}: {e}]</div>"


def _season_number_from_name(filename: str) -> Optional[int]:
	base = os.path.splitext(os.path.basename(filename))[0].strip().lower()
	match = re.match(r"^season[ _-]*0*(\d+)$", base)
	if match:
		return int(match.group(1))
	return None


def _infer_type(filename: str) -> Optional[str]:
	base = os.path.splitext(os.path.basename(filename))[0].strip().lower()
	if _season_number_from_name(filename) is not None:
		return None
	if "backdrop" in base or base.startswith("bd_"):
		return "Backdrop"
	if "banner" in base or base.startswith("bn_"):
		return "Banner"
	if "logo" in base or base.startswith("l_"):
		return "Logo"
	if "thumb" in base or base.startswith("t_"):
		return "Thumb"
	if "clearart" in base or base.startswith("c_"):
		return "Art"
	if "disc" in base or base.startswith("d_"):
		return "Disc"
	if "boxrear" in base or base.startswith("br_"):
		return "BoxRear"
	if "box" in base or base.startswith("b_"):
		return "Box"
	if base in {"poster", "cover", "primary", "folder"} or base.startswith("p_"):
		return "Primary"
	return None


def _get_season_items(server: str, apikey: str, series_id: str) -> Dict[int, Dict]:
	try:
		user_id = _pick_user(server, apikey)
		r = _req(
			"GET",
			f"{server.rstrip('/')}/Users/{user_id}/Items",
			apikey,
			params={
				"ParentId": series_id,
				"IncludeItemTypes": "Season",
				"Recursive": "false",
				"Fields": "IndexNumber,ParentIndexNumber,SortName,Type",
				"SortBy": "SortName",
				"SortOrder": "Ascending",
				"EnableTotalRecordCount": "true",
				"StartIndex": 0,
				"Limit": 1000,
			},
		)
		items = (r.json() or {}).get("Items", []) or []
	except Exception as e:
		log(f"[WARN] Failed to fetch seasons for series {series_id}: {e}")
		return {}

	season_map: Dict[int, Dict] = {}
	for item in items:
		idx = item.get("IndexNumber")
		try:
			if idx is not None:
				season_map[int(idx)] = item
		except Exception:
			continue
	return season_map


def write_restore_report(
	html_path: str,
	base_dir: str,
	results: List[Dict],
	below_threshold: List[Dict],
	unmatched_folders: List[Dict],
	unrestored_items: List[str],
	dry_run: bool,
) -> None:
	ensure_dir(os.path.dirname(html_path))

	with open(html_path, "w", encoding="utf-8") as f:
		title_suffix = " [DRY RUN]" if dry_run else ""

		logo_candidates = [
			"/assets/Pixelfin.png",
			"/app/assets/Pixelfin.png",
			os.path.join(os.path.dirname(__file__), "assets", "Pixelfin.png"),
		]
		logo_path = next((p for p in logo_candidates if os.path.exists(p)), None)

		logo_html = ""
		if logo_path:
			import base64
			with open(logo_path, "rb") as lf:
				logo_b64 = base64.b64encode(lf.read()).decode("utf-8")
			logo_html = (
				"<div style='margin-top:10px;margin-bottom:25px;'>"
				f"<img src='data:image/png;base64,{logo_b64}' alt='Pixelfin Logo' style='height:80px;width:auto;display:block;'>"
				"<a href='/' style='display:inline-block;margin-top:6px;color:#b19cd9;text-decoration:none;font-weight:bold;font-size:1.1em;'>← Back to Main Page</a>"
				"</div>"
			)

		f.write(
			f"<html><head><meta charset='utf-8'>"
			f"<title>Pixelfin Restore Report{title_suffix}</title>"
			"<style>"
			"body{background:#111;color:#eee;font-family:sans-serif;padding:30px;}"
			"table{border-collapse:collapse;width:100%;margin:20px 0;}"
			"th,td{border:1px solid #333;padding:6px;text-align:left;}"
			"th{background:#222;color:#b19cd9;}"
			"tr.matched td{background:#1a2a1a;color:#9f9;}"
			"tr.below td{background:#2a261a;color:#ffb347;}"
			"tr.unmatched td{background:#2a1a1a;color:#f88;}"
			"a{color:#9cf;text-decoration:none;}"
			"a:hover{text-decoration:underline;}"
			".pair{display:flex;gap:25px;justify-content:center;margin:20px 0;flex-wrap:nowrap;overflow-x:auto;}"
			".pair img{max-width:48%;border-radius:10px;cursor:pointer;transition:transform .2s;}"
			".pair img:hover{transform:scale(1.03);}"
			".caption{text-align:center;margin-top:4px;color:#aaa;width:48%;}"
			".item-title{text-align:center;font-size:2.1em;margin-top:50px;margin-bottom:10px;color:#b19cd9;}"
			"</style></head><body>"
		)

		f.write(logo_html)

		if dry_run:
			f.write(
				"<div style='background:#332;padding:14px;border-radius:10px;width:85%;margin:auto;margin-top:20px;margin-bottom:25px;'>"
				"<h2 style='color:#ffb347;margin:0;'>&#x26A0;&#xFE0F; Restore — Simulation Mode</h2>"
				"<p style='margin:6px 0 0 0;'>This is a simulation (dry-run). No changes will be made to your Jellyfin library.</p>"
				"</div>"
			)

		f.write("<h2>Pixelfin Restore Summary</h2>")
		f.write("<h3>Matched (Above Threshold)</h3>")
		f.write("<table><tr><th>ZIP Folder</th><th>Matched Item</th><th>Images</th><th>Score</th></tr>")
		for r in sorted(results, key=lambda r: (r.get("match") or "").lower()):
			folder = r["folder"]
			item = r["match"]
			score = float(r.get("score", 0.0))
			color = score_color(score)
			anchor = safe_basename(item)
			f.write(
				f"<tr class='matched'><td>{folder}</td>"
				f"<td><a href='#{anchor}'>{item}</a></td>"
				f"<td>{len(r.get('images', []))}</td>"
				f"<td style='color:{color};font-weight:bold;'>{score:.2f}</td></tr>"
			)
		f.write("</table>")

		f.write("<h3>Below Threshold (Manual Review)</h3>")
		if below_threshold:
			f.write("<table><tr><th>Folder</th><th>Closest Match</th><th>Similarity</th></tr>")
			for u in sorted(below_threshold, key=lambda x: (x.get("folder") or "").lower()):
				f.write(
					f"<tr class='below'><td>{u.get('folder','')}</td>"
					f"<td>{u.get('best_match','—')}</td>"
					f"<td>{u.get('similarity','0')}%</td></tr>"
				)
			f.write("</table>")
		else:
			f.write("<p>None</p>")

		f.write("<h3>Unmatched ZIP Folders (No Plausible Library Match)</h3>")
		if unmatched_folders:
			f.write("<table><tr><th>Folder</th><th>Closest Match</th><th>Similarity</th></tr>")
			for u in sorted(unmatched_folders, key=lambda x: (x.get("folder") or "").lower()):
				f.write(
					f"<tr class='unmatched'><td>{u.get('folder','')}</td>"
					f"<td>{u.get('best_match','—')}</td>"
					f"<td>{u.get('similarity','0')}%</td></tr>"
				)
			f.write("</table>")
		else:
			f.write("<p>None</p>")

		f.write("<h2>Before / After Comparisons</h2>")
		for r in results:
			item = r["match"]
			anchor = safe_basename(item)
			folder_path = os.path.join(base_dir, r["folder"])

			after_files = [
				os.path.join(folder_path, img)
				for img in r.get("images", [])
				if os.path.exists(os.path.join(folder_path, img))
			]
			before_files = r.get("before_images", [])

			if not after_files and not before_files:
				continue

			f.write(f"<div id='{anchor}' class='item-title'>{item}</div>")

			for img_path in after_files:
				img_name = os.path.basename(img_path)
				img_type = _infer_type(img_name) or "Primary"
				img_type_lower = img_type.lower()
				before_match = next(
					(
						bf for bf in before_files
						if bf and img_type_lower in os.path.basename(bf).lower()
					),
					None,
				)

				f.write("<div class='pair' style='display:flex;justify-content:center;align-items:center;gap:60px;margin:40px 0;flex-wrap:nowrap;'>")

				if before_match and os.path.exists(before_match):
					f.write(
						"<div style='text-align:center;flex:1;'>"
						f"{embed_image(before_match, f'Before ({img_type})', css='compare-img')}"
						"</div>"
					)
				else:
					f.write(f"<div style='text-align:center;flex:1;color:#888;'>No current {img_type}</div>")

				caption = f"Would be After ({img_type})" if dry_run else f"After ({img_type})"
				f.write(
					"<div style='text-align:center;flex:1;'>"
					f"{embed_image(img_path, caption, css='compare-img')}"
					"</div>"
				)

				f.write("</div>")

		f.write("</body></html>")

	log(f"[INFO] Wrote rich comparison HTML -> {html_path}")


# =============================================================================
# Core Restore Logic
# =============================================================================
def _descend_wrapper_folder(base_dir: str) -> str:
	while True:
		subdirs = [
			os.path.join(base_dir, d) for d in os.listdir(base_dir)
			if os.path.isdir(os.path.join(base_dir, d))
			and not d.startswith(".") and not d.startswith("__MACOSX")
		]
		if len(subdirs) == 1:
			next_dir = subdirs[0]
			inner_dirs = [
				os.path.join(next_dir, d) for d in os.listdir(next_dir)
				if os.path.isdir(os.path.join(next_dir, d))
				and not d.startswith(".") and not d.startswith("__MACOSX")
			]
			if inner_dirs:
				log(f"[INFO] Wrapper folder detected -> diving into: {next_dir}")
				base_dir = next_dir
				continue
		break
	return base_dir


def _scan_media_folders(base_dir: str) -> List[str]:
	return [
		f for f in os.listdir(base_dir)
		if os.path.isdir(os.path.join(base_dir, f))
		and not f.startswith(".")
		and not f.startswith("__MACOSX")
	]


def run_restore(
	path: str,
	library: str,
	threshold: float = 0.95,
	dry_run: bool = False,
	comparison_html: bool = False,
	server: str = "",
	apikey: str = "",
	forced_mappings: Optional[Dict[str, str]] = None,
) -> Dict:
	"""
	Perform restore from images into Jellyfin; optional HTML report.

	Guarantee: every ZIP folder ends up in exactly one bucket:
	  - matched (>= threshold, or forced match)
	  - below_threshold (best candidate exists but < threshold AND >= unmatched_floor)
	  - unmatched_folders (no plausible match; best_score < unmatched_floor)

	Back-compat:
	  - "matches" = full detail matches list
	  - "unmatched" = alias of below_threshold (older templates)
	  - "matched" = simplified list for review template
	"""
	log(f"[DEBUG] RESTORE FILE: {__file__}")
	log("[DEBUG] RESTORE VERSION: v0.2.2")

	if not server or not apikey:
		log("[ERROR] Missing server or API key. Aborting restore.")
		return {"status": "error", "message": "Missing server or API key."}

	unmatched_floor = _DEFAULT_UNMATCHED_FLOOR
	try:
		if os.environ.get("PIXELFIN_UNMATCHED_FLOOR"):
			unmatched_floor = float(os.environ["PIXELFIN_UNMATCHED_FLOOR"])
			unmatched_floor = max(0.0, min(1.0, unmatched_floor))
	except Exception:
		unmatched_floor = _DEFAULT_UNMATCHED_FLOOR

	tmpdir: Optional[str] = None
	forced_mappings = forced_mappings or {}

	try:
		if os.path.isfile(path) and path.lower().endswith(".zip"):
			tmpdir = tempfile.mkdtemp(prefix="pixelfin_restore_")
			with zipfile.ZipFile(path, "r") as zf:
				zf.extractall(tmpdir)
			base_dir = tmpdir
			log(f"[INFO] Unpacked ZIP to {base_dir}")
		else:
			base_dir = path

		if not os.path.isdir(base_dir):
			return {"status": "error", "message": f"Invalid path: {path}"}

		base_dir = _descend_wrapper_folder(base_dir)
		folders = _scan_media_folders(base_dir)
		if not folders:
			return {"status": "error", "message": f"No media folders found in {base_dir}"}
		log(f"[INFO] Found {len(folders)} media folders under {base_dir}")

		items, collection_type = get_library_items(server, apikey, library)
		if not items:
			return {
				"status": "error",
				"message": f"Library '{library}' returned 0 items (fetch completed). Check server/apikey/library name.",
			}

		items_by_norm_name: Dict[str, Dict] = {}
		for item in items:
			name = (item.get("Name") or "").strip()
			if name:
				items_by_norm_name.setdefault(_normalize_title(name), item)

		all_titles = sorted(
			{(i.get("Name") or "").strip() for i in items if (i.get("Name") or "").strip()},
			key=lambda s: s.lower(),
		)
		log(f"[INFO] Library collection type: '{collection_type or 'unknown'}'")
		log(f"[INFO] Titles available for dropdown: {len(all_titles)}")

		results: List[Dict] = []
		below_threshold: List[Dict] = []
		unmatched_folders: List[Dict] = []

		for folder in sorted(folders, key=lambda s: s.lower()):
			folder_path = os.path.join(base_dir, folder)

			forced_title = (forced_mappings.get(folder) or "").strip()
			forced_item: Optional[Dict] = None
			if forced_title:
				forced_item = items_by_norm_name.get(_normalize_title(forced_title))
				if forced_item:
					log(f"[INFO] Forced mapping: '{folder}' -> '{forced_item.get('Name')}'")
				else:
					log(f"[WARN] Forced mapping for '{folder}' did not resolve to a library item: '{forced_title}'")

			best_item: Optional[Dict] = forced_item
			best_score = 1.0 if forced_item else -1.0

			for item in items:
				name = item.get("Name", "") or ""
				score, _, _ = _match_components(folder, name)
				if score > best_score:
					best_score = score
					best_item = item

			best_name = (best_item.get("Name") if best_item else None)

			if forced_item is None:
				if best_item is None or best_score < unmatched_floor:
					unmatched_folders.append({
						"folder": folder,
						"best_match": best_name,
						"similarity": round(max(best_score, 0.0) * 100, 2),
					})
					continue

				if best_score < threshold:
					below_threshold.append({
						"folder": folder,
						"best_match": best_name,
						"similarity": round(best_score * 100, 2),
					})
					continue

			item_id = best_item["Id"]
			item_name = best_name or folder

			image_files = sorted([
				f for f in os.listdir(folder_path)
				if os.path.splitext(f)[1].lower() in (".jpg", ".jpeg", ".png")
			], key=lambda s: s.lower())
			if not image_files:
				continue

			before_dir = os.path.join(tempfile.gettempdir(), "pixelfin_before")
			ensure_dir(before_dir)
			before_images: List[str] = []
			season_items = _get_season_items(server, apikey, item_id) if (best_item.get("Type") == "Series") else {}

			for img in image_files:
				season_number = _season_number_from_name(img)
				if season_number is not None:
					season_item = season_items.get(season_number)
					if not season_item:
						continue
					before_url = f"{server.rstrip('/')}/Items/{season_item['Id']}/Images/Primary"
					before_path = os.path.join(before_dir, f"{safe_basename(item_name)}_Season{season_number:02d}_Primary_before.jpg")
				else:
					img_type = _infer_type(img)
					if not img_type:
						continue
					before_url = f"{server.rstrip('/')}/Items/{item_id}/Images/{img_type}"
					before_path = os.path.join(before_dir, f"{safe_basename(item_name)}_{img_type}_before.jpg")
				try:
					r = SESSION.get(
						before_url,
						headers={"X-Emby-Token": apikey, "User-Agent": USER_AGENT},
						timeout=_DEFAULT_TIMEOUT,
					)
					if r.ok and r.content:
						with open(before_path, "wb") as bf:
							bf.write(r.content)
						before_images.append(before_path)
				except Exception:
					pass

			if dry_run:
				results.append({
					"folder": folder,
					"match": item_name,
					"images": image_files,
					"before_images": before_images,
					"score": round(best_score, 4),
					"similarity": round(best_score * 100, 2),
				})
				continue

			backdrops = [f for f in image_files if "backdrop" in f.lower()]
			others = [f for f in image_files if f not in backdrops]
			ordered = others + backdrops

			for img in ordered:
				image_path = os.path.join(folder_path, img)
				season_number = _season_number_from_name(img)
				if season_number is not None:
					season_item = season_items.get(season_number)
					if not season_item:
						log(f"[WARN] No matching season found for {item_name}: {img}")
						continue
					delete_images(server, apikey, season_item["Id"], "Primary")
					upload_image(server, apikey, season_item["Id"], "Primary", image_path)
					continue
				img_type = _infer_type(img)
				if not img_type:
					log(f"[INFO] Skipping unrecognized image name for {item_name}: {img}")
					continue
				delete_images(server, apikey, item_id, img_type)
				upload_image(server, apikey, item_id, img_type, image_path)

			results.append({
				"folder": folder,
				"match": item_name,
				"images": ordered,
				"before_images": before_images,
				"score": round(best_score, 4),
				"similarity": round(best_score * 100, 2),
			})

		matched_names = {m["match"] for m in results}
		unrestored_items = [i.get("Name") for i in items if i.get("Name") and i.get("Name") not in matched_names]

		summary = {
			"folders_total": len(folders),
			"matched_folders": len(results),
			"below_threshold_folders": len(below_threshold),
			"unmatched_folders": len(unmatched_folders),
			"unmatched_floor": unmatched_floor,
			"library_items_total": len(items),
			"library_items_unrestored": unrestored_items,
		}

		html_path = None
		if comparison_html:
			base_output = os.environ.get("PIXELFIN_BASE_OUTPUT", "/app/output")
			safe_library = re.sub(r"[^A-Za-z0-9_\-]", "_", library or "RestoreReports")
			out_dir = os.path.join(base_output, safe_library)
			ensure_dir(out_dir)

			existing = [fn for fn in os.listdir(out_dir) if fn.lower().startswith("restore-") and fn.lower().endswith(".html")]
			for fn in existing:
				is_dry = "dry-run" in fn.lower()
				if (is_dry and dry_run) or ((not is_dry) and (not dry_run)):
					try:
						os.remove(os.path.join(out_dir, fn))
					except Exception:
						pass

			timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
			suffix = "_Dry-Run" if dry_run else ""
			html_path = os.path.join(out_dir, f"Restore-{timestamp}{suffix}.html")

			write_restore_report(
				html_path=html_path,
				base_dir=base_dir,
				results=results,
				below_threshold=below_threshold,
				unmatched_folders=unmatched_folders,
				unrestored_items=unrestored_items,
				dry_run=dry_run,
			)

		return {
			"status": "ok",
			"matches": results,
			"unmatched": below_threshold,
			"summary": summary,
			"comparison_html": html_path,
			"logs": [],
			"matched": [
				{"folder": m["folder"], "best_match": m["match"], "similarity": m["similarity"]}
				for m in results
			],
			"below_threshold": below_threshold,
			"unmatched_folders": unmatched_folders,
			"all_matches": all_titles,
		}

	except Exception as e:
		import traceback
		log(f"[FATAL] {e}")
		log(traceback.format_exc())
		return {"status": "error", "message": str(e)}

	finally:
		if tmpdir and os.path.exists(tmpdir):
			shutil.rmtree(tmpdir, ignore_errors=True)


# =============================================================================
# Streaming wrapper
# =============================================================================
def run_restore_streamed(**kwargs):
	import io
	import contextlib

	buffer = io.StringIO()
	with contextlib.redirect_stdout(buffer):
		result = run_restore(**kwargs)

	for line in buffer.getvalue().splitlines():
		yield line + "\n"

	yield "\n=== RESTORE COMPLETE ===\n"
	yield json.dumps(result, indent=2) + "\n"


# =============================================================================
# CLI
# =============================================================================
def _parse_args(argv: Optional[List[str]] = None):
	import argparse
	p = argparse.ArgumentParser(description="Pixelfin Restore Engine (Jellyfin)")
	p.add_argument("path", help="Path to ZIP or directory containing media folders")
	p.add_argument("library", help="Jellyfin library name")
	p.add_argument("--server", help="Jellyfin server URL (e.g. http://host:8096)", required=False, default=os.environ.get("PIXELFIN_SERVER", ""))
	p.add_argument("--apikey", help="Jellyfin API key", required=False, default=os.environ.get("PIXELFIN_API_KEY", ""))
	p.add_argument("--threshold", type=float, default=0.75, help="Fuzzy match threshold (0..1)")
	p.add_argument("--dry-run", action="store_true", help="Do not modify Jellyfin; only compare")
	p.add_argument("--comparison-html", action="store_true", help="Generate comparison HTML report")
	return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
	args = _parse_args(argv)
	result = run_restore(
		path=args.path,
		library=args.library,
		threshold=args.threshold,
		dry_run=args.dry_run,
		comparison_html=args.comparison_html,
		server=args.server,
		apikey=args.apikey,
	)
	print(json.dumps(result, indent=2))
	return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
	raise SystemExit(main())