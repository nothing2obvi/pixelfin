# app.py
from flask import (
	Flask,
	request,
	render_template,
	Response,
	send_from_directory,
	redirect,
	url_for,
	stream_with_context,
	has_request_context,
)
import subprocess
import os
import logging
import threading
import queue
import json
from datetime import datetime
from zoneinfo import ZoneInfo
import base64
import re
import requests
import shutil
import zipfile
import uuid
from urllib.parse import quote
from restore import run_restore, run_restore_streamed
from io import BytesIO
from generate_html import add_jellytag_bypass as generate_add_jellytag_bypass
from generate_html import check_low_res
import fresh_state
from fresh_jellyfin import (
	DEFAULT_SELECTED_IMAGES,
	DEFAULT_THRESHOLDS,
	DEFAULT_ZIP_BASENAMES as FRESH_DEFAULT_ZIP_BASENAMES,
	IMAGE_TYPE_OPTIONS as FRESH_IMAGE_TYPE_OPTIONS,
	is_supported_library,
	list_views,
	scan_library,
	scan_media_item,
	test_server,
)

# ---------------------------------------------------------------------
# Force all paths to resolve relative to this file (like the old system)
# ---------------------------------------------------------------------
os.chdir(os.path.dirname(os.path.abspath(__file__)))

HISTORY_FILE = "data/history.json"
AUTO_FILE = "data/auto_jobs.json"
KEEP_FILE = "data/keep.json"  # ✅ NEW: Keep/Dont-Keep storage

app = Flask(__name__, template_folder="templates")
BASE_OUTPUT_DIR = "output"
ASSETS_DIR = "assets"
FRESH_COVER_CACHE_DIR = os.path.join("data", "fresh_cover_cache")
FRESH_SCAN_JOBS = {}
FRESH_SCAN_JOBS_LOCK = threading.Lock()

os.makedirs("data", exist_ok=True)
os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)
os.makedirs(FRESH_COVER_CACHE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
app.logger.addHandler(logging.StreamHandler())

IMAGE_TYPE_OPTIONS = {
	"p": "Primary",
	"c": "ClearArt",
	"bd": "Backdrop",
	"bn": "Banner",
	"b": "Box",
	"br": "BoxRear",
	"d": "Disc",
	"l": "Logo",
	"m": "Menu",
	"t": "Thumb",
}

DEFAULT_ZIP_BASENAMES = {
	"p": "cover",
	"t": "thumbnail",
	"bd": "backdrop",
	"c": "clearart",
	"bn": "banner",
	"b": "box",
	"br": "boxrear",
	"d": "disc",
	"l": "logo",
	"m": "menu",
}


# ----------------- Custom Jinja Filters -----------------
@app.template_filter("basename")
def basename_filter(path):
	if not path:
		return ""
	return os.path.basename(path)


def load_pixelfin_base64(filename):
	path = os.path.join(ASSETS_DIR, filename)
	if os.path.exists(path):
		with open(path, "rb") as f:
			return base64.b64encode(f.read()).decode("utf-8")
	return ""


PIXELFIN_BASE64 = load_pixelfin_base64("Pixelfin.png")
PIXELFIN_FAVICON_BASE64 = load_pixelfin_base64("Pixelfin_Favicon.png")


# ----------------- Keep Helpers (NEW) -----------------
def _ensure_keep_file():
	if not os.path.exists(KEEP_FILE):
		with open(KEEP_FILE, "w", encoding="utf-8") as f:
			json.dump({"kept": {}}, f, indent=2)


def load_keep():
	_ensure_keep_file()
	try:
		with open(KEEP_FILE, "r", encoding="utf-8") as f:
			data = json.load(f)
		if not isinstance(data, dict):
			raise ValueError("keep file not dict")
	except Exception:
		data = {"kept": {}}

	data.setdefault("kept", {})
	if not isinstance(data["kept"], dict):
		data["kept"] = {}
	return data


def save_keep(data: dict):
	_ensure_keep_file()
	with open(KEEP_FILE, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2)


def is_file_kept(library_folder: str, filename: str) -> bool:
	data = load_keep()
	lib_map = data.get("kept", {}).get(library_folder, {})
	return bool(lib_map.get(filename))


def toggle_keep_file(library_folder: str, filename: str) -> bool:
	"""
	Returns NEW state: True if kept after toggle, False if not kept after toggle.
	Auto-cleans missing files from keep map when toggling.
	"""
	data = load_keep()
	data.setdefault("kept", {})
	data["kept"].setdefault(library_folder, {})
	current = bool(data["kept"][library_folder].get(filename))

	# flip
	new_state = not current
	if new_state:
		data["kept"][library_folder][filename] = True
	else:
		try:
			del data["kept"][library_folder][filename]
		except Exception:
			pass

	# clean empty
	if not data["kept"][library_folder]:
		try:
			del data["kept"][library_folder]
		except Exception:
			pass

	save_keep(data)
	return new_state


def get_kept_filenames_for_library(library_folder: str) -> set:
	data = load_keep()
	lib_map = data.get("kept", {}).get(library_folder, {})
	if not isinstance(lib_map, dict):
		return set()
	return set([k for k, v in lib_map.items() if v])


# ----------------- History Helpers -----------------
def load_history():
	# Ensure file exists and is a file, not a folder
	if os.path.exists(HISTORY_FILE) and os.path.isdir(HISTORY_FILE):
		shutil.rmtree(HISTORY_FILE)
		with open(HISTORY_FILE, "w", encoding="utf-8") as f:
			f.write("{}")

	if not os.path.exists(HISTORY_FILE):
		with open(HISTORY_FILE, "w", encoding="utf-8") as f:
			f.write("{}")

	with open(HISTORY_FILE, "r", encoding="utf-8") as f:
		try:
			data = json.load(f)
			if not isinstance(data, dict):
				raise ValueError("history is not a dict")
		except Exception:
			data = {"servers": [], "libraries": [], "library_settings": {}, "last_used": {}}

	data.setdefault("servers", [])
	data.setdefault("libraries", [])
	data.setdefault("library_settings", {})
	data.setdefault("last_used", {})
	return data


def save_history(server, library, settings):
	history = load_history()

	if server and server not in history.get("servers", []):
		history.setdefault("servers", []).append(server)

	if library and library not in history.get("libraries", []):
		history.setdefault("libraries", []).append(library)

	if library:
		history.setdefault("library_settings", {})[library] = settings

	history["last_used"] = {
		"server": server,
		"apikey": settings.get("apikey", ""),
		"images": settings.get("images", list(IMAGE_TYPE_OPTIONS.keys())),
		"minres": settings.get("minres", {}),
		"zipnames": settings.get("zipnames", {}),
		"bgcolor": settings.get("bgcolor", "#000000"),
		"textcolor": settings.get("textcolor", "#ffffff"),
		"tablebgcolor": settings.get("tablebgcolor", "#000000"),
		"sort_order": settings.get("sort_order", "alphabetical"),
		"jellytag_bypass": bool(settings.get("jellytag_bypass", False)),
	}

	with open(HISTORY_FILE, "w", encoding="utf-8") as f:
		json.dump(history, f, indent=2)


# ----------------- Auto Jobs Helpers -----------------
def _ensure_auto_file():
	if not os.path.exists(AUTO_FILE):
		with open(AUTO_FILE, "w", encoding="utf-8") as f:
			json.dump({"cron": "", "jobs": [], "last_run_minute": ""}, f, indent=2)


def load_auto():
	_ensure_auto_file()
	try:
		with open(AUTO_FILE, "r", encoding="utf-8") as f:
			data = json.load(f)
		if not isinstance(data, dict):
			raise ValueError("auto file not dict")
	except Exception:
		data = {"cron": "", "jobs": [], "last_run_minute": ""}

	data.setdefault("cron", "")
	data.setdefault("jobs", [])
	data.setdefault("last_run_minute", "")
	data.setdefault("fresh_global_zip", False)
	data.setdefault("fresh_keep_zip", 2)
	data.setdefault("fresh_scan_cron", "")
	data.setdefault("fresh_scan_last_run_minute", "")

	norm = []
	for j in data["jobs"]:
		if not isinstance(j, dict):
			continue
		jj = {
			"library": (j.get("library") or "").strip(),
			"auto_html": bool(j.get("auto_html", True)),
			"keep_html": int(j.get("keep_html", 2) or 0),
			"auto_zip": bool(j.get("auto_zip", False)),
			"keep_zip": int(j.get("keep_zip", 2) or 0),
			"images": j.get("images") or list(IMAGE_TYPE_OPTIONS.keys()),
			"minres": j.get("minres") or {},
			"zipnames": j.get("zipnames") or {},
			"sort_order": (j.get("sort_order") or "alphabetical").strip() or "alphabetical",
			"jellytag_bypass": bool(j.get("jellytag_bypass", False)),
		}
		if jj["sort_order"] not in ("alphabetical", "recent"):
			jj["sort_order"] = "alphabetical"
		norm.append(jj)
	data["jobs"] = norm
	return data


def save_auto(payload: dict):
	_ensure_auto_file()
	with open(AUTO_FILE, "w", encoding="utf-8") as f:
		json.dump(payload, f, indent=2)


# ----------------- Output listing helpers -----------------
def list_generated_htmls():
	"""
	Returns:
	  { display_library_name: [ {filename, name, path, folder, is_kept}, ... ] }
	"""
	result = {}
	history = load_history()
	if not os.path.exists(BASE_OUTPUT_DIR):
		return result

	for folder in sorted(os.listdir(BASE_OUTPUT_DIR)):
		lib_folder = os.path.join(BASE_OUTPUT_DIR, folder)
		if not os.path.isdir(lib_folder):
			continue

		files = []
		for f in sorted(os.listdir(lib_folder), reverse=True):
			lower_f = f.lower()
			if lower_f.endswith(".html") or lower_f.endswith(".zip"):
				files.append(
					{
						"filename": f,
						"name": f,
						"path": f"/output/{quote(folder)}/{quote(f)}",
						"folder": folder,
						"is_kept": is_file_kept(folder, f),
					}
				)

		if files:
			display_name = next(
				(lib for lib in history.get("libraries", []) if lib.replace(" ", "") == folder),
				folder,
			)
			result[display_name] = files

	# include restore htmls if present
	for folder in sorted(os.listdir(BASE_OUTPUT_DIR)):
		lib_folder = os.path.join(BASE_OUTPUT_DIR, folder)
		if not os.path.isdir(lib_folder):
			continue

		restore_htmls = [
			f
			for f in sorted(os.listdir(lib_folder), reverse=True)
			if f.lower().startswith("restore-") and f.lower().endswith(".html")
		]
		if restore_htmls:
			files = [
				{
					"filename": f,
					"name": f,
					"path": f"/output/{quote(folder)}/{quote(f)}",
					"folder": folder,
					"is_kept": is_file_kept(folder, f),
				}
				for f in restore_htmls
			]
			result[folder] = result.get(folder, []) + files

	return result


def list_zip_files():
	"""
	Return all .zip files in /app/output (recursive), but sorted as:
	  - library folder alphabetical
	  - within each library, newest zip first
	Returned values remain the same strings used elsewhere:
	  "LibraryFolder/filename.zip" (or "filename.zip" if top-level)
	"""
	if not os.path.isdir(BASE_OUTPUT_DIR):
		return []

	entries = []
	for root, _, files in os.walk(BASE_OUTPUT_DIR):
		for f in files:
			if not f.lower().endswith(".zip"):
				continue
			full = os.path.join(root, f)
			try:
				mtime = os.path.getmtime(full)
			except Exception:
				mtime = 0

			rel_dir = os.path.relpath(root, BASE_OUTPUT_DIR)
			rel_path = os.path.join(rel_dir, f) if rel_dir != "." else f

			# "library" is the top folder in output (or "" if none)
			parts = rel_path.split(os.sep)
			lib_key = parts[0] if len(parts) > 1 else ""
			entries.append((lib_key.lower(), lib_key, -mtime, rel_path))

	# Sort: library alpha (case-insensitive), then newest first (mtime desc via -mtime), then filename
	entries.sort(key=lambda x: (x[0], x[2], x[3].lower()))
	return [e[3] for e in entries]


def now_in_tz():
	tzname = os.environ.get("PIXELFIN_TIMEZONE") or os.environ.get("TIMEZONE") or os.environ.get("TZ")
	try:
		if tzname:
			return datetime.now(ZoneInfo(tzname))
		return datetime.now().astimezone()
	except Exception:
		return datetime.now()


def _stream_page_open(title: str):
	return (
		f"<html><head><title>{title}</title>"
		f"<link rel='icon' type='image/png' href='data:image/png;base64,{PIXELFIN_FAVICON_BASE64}' />"
		"<style>"
		"body{margin:0;padding:18px;background:#fff;color:#111;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;}"
		"pre{overflow:auto;max-height:520px;background:#0b0b0b;color:#0f0;padding:14px;border-radius:10px;border:1px solid #333;}"
		".actions{margin-top:14px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;}"
		".btn{display:inline-block;padding:10px 14px;border-radius:10px;border:1px solid #ccc;background:#f5f5f5;color:#111;text-decoration:none;}"
		".btn:hover{background:#eee;}"
		".muted{color:#666;font-size:12px;margin-top:10px;}"
		"</style></head><body>"
		"<pre id='log'>\n"
	)


def _stream_page_close():
	return "<script>var pre=document.getElementById('log');pre.scrollTop=pre.scrollHeight;</script></body></html>"


# ----------------- Cron parsing + scheduler -----------------
_CRON_RANGES = {
	"min": (0, 59),
	"hour": (0, 23),
	"dom": (1, 31),
	"month": (1, 12),
	"dow": (0, 6),  # 0=Sunday
}


def _parse_cron_field(field: str, lo: int, hi: int):
	field = (field or "").strip()
	if not field:
		return None, False

	any_star = field == "*"
	values = set()

	def add_range(a, b, step=1):
		a = max(lo, a)
		b = min(hi, b)
		for v in range(a, b + 1, step):
			values.add(v)

	for part in field.split(","):
		part = part.strip()
		if not part:
			continue

		if part == "*":
			add_range(lo, hi, 1)
			continue

		if part.startswith("*/"):
			step = int(part[2:])
			add_range(lo, hi, step)
			continue

		# handle a-b or a-b/n
		m = re.match(r"^(\d+)-(\d+)(?:/(\d+))?$", part)
		if m:
			a = int(m.group(1))
			b = int(m.group(2))
			step = int(m.group(3)) if m.group(3) else 1
			add_range(a, b, step)
			continue

		# handle single number
		if re.match(r"^\d+$", part):
			v = int(part)
			if lo <= v <= hi:
				values.add(v)
			continue

		# unknown token => invalid
		return None, any_star

	return values, any_star


def cron_matches(dt: datetime, expr: str) -> bool:
	"""
	Supports 5-field cron: minute hour day-of-month month day-of-week
	- *, */n, lists, ranges, range/step
	- DOW: 0=Sunday ... 6=Saturday
	- DOM & DOW behavior: Vixie-style OR when both are restricted (not '*')
	"""
	expr = (expr or "").strip()
	if not expr:
		return False

	parts = re.split(r"\s+", expr)
	if len(parts) != 5:
		return False

	mins, hours, doms, months, dows = parts

	mins_set, _ = _parse_cron_field(mins, *_CRON_RANGES["min"])
	hours_set, _ = _parse_cron_field(hours, *_CRON_RANGES["hour"])
	dom_set, dom_star = _parse_cron_field(doms, *_CRON_RANGES["dom"])
	month_set, _ = _parse_cron_field(months, *_CRON_RANGES["month"])
	dow_set, dow_star = _parse_cron_field(dows, *_CRON_RANGES["dow"])

	if mins_set is None or hours_set is None or dom_set is None or month_set is None or dow_set is None:
		return False

	# map python weekday (Mon=0..Sun=6) -> cron dow (Sun=0..Sat=6)
	cron_dow = (dt.weekday() + 1) % 7

	if dt.minute not in mins_set:
		return False
	if dt.hour not in hours_set:
		return False
	if dt.month not in month_set:
		return False

	dom_match = dt.day in dom_set
	dow_match = cron_dow in dow_set

	if dom_star and dow_star:
		return True
	if dom_star and not dow_star:
		return dow_match
	if dow_star and not dom_star:
		return dom_match
	# both restricted => OR semantics
	return dom_match or dow_match


def _safe_library_folder(library: str) -> str:
	return re.sub(r"[^A-Za-z0-9_\-]", "_", library or "")


def _newest_file_in_folder(lib_folder: str, exts=(".html",), exclude_prefixes=()):
	"""
	Return newest file (by mtime) in folder matching exts, excluding prefixes.
	This is the source of truth for what was actually created.
	"""
	try:
		if not os.path.isdir(lib_folder):
			return None
		best = None
		best_m = -1
		for f in os.listdir(lib_folder):
			lf = f.lower()
			if not any(lf.endswith(ext) for ext in exts):
				continue
			if any(lf.startswith(p.lower()) for p in exclude_prefixes):
				continue
			path = os.path.join(lib_folder, f)
			try:
				m = os.path.getmtime(path)
			except Exception:
				continue
			if m > best_m:
				best_m = m
				best = f
		return best
	except Exception:
		return None


def _prune_outputs_for_library(library: str, keep_html: int, keep_zip: int):
	"""
	keep_* of 0 => unlimited (no prune).
	Applies to the same output folder used by the manual tab, so it prunes across BOTH.

	✅ NEW RULE:
	  Files marked "Keep" are excluded from pruning and remain in addition to the keep limits.
	"""
	safe_lib = _safe_library_folder(library)
	lib_folder = os.path.join(BASE_OUTPUT_DIR, safe_lib)
	if not os.path.isdir(lib_folder):
		return

	items = os.listdir(lib_folder)
	kept = get_kept_filenames_for_library(safe_lib)

	# HTML (exclude restore-*.html)
	if keep_html and keep_html > 0:
		htmls = [
			f for f in items
			if f.lower().endswith(".html")
			and not f.lower().startswith("restore-")
			and f not in kept
		]
		htmls_sorted = sorted(
			htmls,
			key=lambda x: os.path.getmtime(os.path.join(lib_folder, x)),
			reverse=True,
		)
		for f in htmls_sorted[keep_html:]:
			try:
				os.remove(os.path.join(lib_folder, f))
			except Exception:
				pass

	# ZIP
	if keep_zip and keep_zip > 0:
		zips = [
			f for f in items
			if f.lower().endswith(".zip")
			and f not in kept
		]
		zips_sorted = sorted(
			zips,
			key=lambda x: os.path.getmtime(os.path.join(lib_folder, x)),
			reverse=True,
		)
		for f in zips_sorted[keep_zip:]:
			try:
				os.remove(os.path.join(lib_folder, f))
			except Exception:
				pass

	# remove empty folder (only if truly empty)
	try:
		if os.path.isdir(lib_folder) and not os.listdir(lib_folder):
			os.rmdir(lib_folder)
	except Exception:
		pass


def _run_generate_html_once(server, apikey, library, bgcolor, textcolor, tablebgcolor, images, minres, zipnames, sort_order, jellytag_bypass=False):
	safe_lib = _safe_library_folder(library)
	lib_folder = os.path.join(BASE_OUTPUT_DIR, safe_lib)
	os.makedirs(lib_folder, exist_ok=True)

	now = now_in_tz()
	timestamp_file = now.strftime("%Y-%m-%d_%H-%M-%S")
	timestamp_html = now.strftime("%Y-%m-%d %H:%M:%S")
	sort_suffix = "Alphabetical" if sort_order == "alphabetical" else "Date-Added"

	html_filename = f"{timestamp_file} - {library} - {sort_suffix}.html"
	output_file = os.path.join(lib_folder, html_filename)

	args = [
		"python",
		"generate_html.py",
		"--server", server,
		"--apikey", apikey,
		"--library", library,
		"--output", output_file,
		"--bgcolor", bgcolor,
		"--textcolor", textcolor,
		"--tablebgcolor", tablebgcolor,
		"--images", ",".join(images),
		"--timestamp", timestamp_html,
		"--sort", sort_order,
	]
	if minres:
		minres_str = ";".join([f"{code}:{int(v[0])}x{int(v[1])}" for code, v in minres.items()])
		args += ["--minres", minres_str]
	if jellytag_bypass:
		args.append("--jellytag-bypass")

	app.logger.info("AUTO: running HTML for library=%s sort=%s", library, sort_order)
	proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

	if proc.returncode != 0:
		app.logger.error("AUTO: HTML failed for %s\n%s", library, proc.stdout or "")
	return proc.returncode == 0


def _run_generate_zip_once(server, apikey, library, images, zipnames, sort_order, jellytag_bypass=False):
	safe_lib = _safe_library_folder(library)
	lib_folder = os.path.join(BASE_OUTPUT_DIR, safe_lib)
	os.makedirs(lib_folder, exist_ok=True)

	now = now_in_tz()
	timestamp_file = now.strftime("%Y-%m-%d_%H-%M-%S")

	zip_filename = f"{timestamp_file} - {library}.zip"
	zip_path = os.path.join(lib_folder, zip_filename)

	args = [
		"python",
		"generate_html.py",
		"--server", server,
		"--apikey", apikey,
		"--library", library,
		"--images", ",".join(images),
		"--zip-output", zip_path,
		"--zipnames", json.dumps(zipnames or {}),
		"--sort", sort_order,
	]
	if jellytag_bypass:
		args.append("--jellytag-bypass")

	app.logger.info("AUTO: running ZIP for library=%s sort=%s", library, sort_order)
	proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
	if proc.returncode != 0:
		app.logger.error("AUTO: ZIP failed for %s\n%s", library, proc.stdout or "")
	return proc.returncode == 0


def _run_auto_sequence():
	try:
		conn = _fresh_conn()
		fresh_auto = load_auto()
		if fresh_auto.get("fresh_global_zip"):
			server = _fresh_active_server(conn)
			if server:
				keep_zip = int(fresh_auto.get("fresh_keep_zip") or 0)
				for library in _fresh_libraries(conn, server["id"]):
					try:
						_lib, images, _thresholds, zipnames = _fresh_library_export_settings(conn, server, library["id"])
						_run_generate_zip_once(
							server=server["url"],
							apikey=server["api_key"],
							library=library["name"],
							images=images,
							zipnames=zipnames,
							sort_order="alphabetical",
							jellytag_bypass=_fresh_jellytag_enabled(conn),
						)
						_prune_outputs_for_library(library["name"], keep_html=0, keep_zip=keep_zip)
					except Exception as e:
						app.logger.exception("FRESH AUTO: ZIP failed for %s: %s", library.get("name"), e)
			return
	except Exception:
		app.logger.exception("FRESH AUTO: failed before legacy auto fallback")

	history = load_history()
	auto = load_auto()

	last_used = history.get("last_used", {})
	server = last_used.get("server", "")
	apikey = last_used.get("apikey", "")
	bgcolor = last_used.get("bgcolor", "#000000")
	textcolor = last_used.get("textcolor", "#ffffff")
	tablebgcolor = last_used.get("tablebgcolor", "#000000")
	default_sort = last_used.get("sort_order", "alphabetical")

	if not server or not apikey:
		app.logger.warning("AUTO: missing last-used server/apikey; open main tab once and save settings.")
		return

	for job in auto.get("jobs", []):
		library = (job.get("library") or "").strip()
		if not library:
			continue

		lib_settings = history.get("library_settings", {}).get(library, {})
		job_images = job.get("images") or lib_settings.get("images") or last_used.get("images") or list(IMAGE_TYPE_OPTIONS.keys())
		job_minres = job.get("minres") or lib_settings.get("minres") or {}
		job_zipnames = job.get("zipnames") or lib_settings.get("zipnames") or {}
		job_jellytag_bypass = bool(job.get("jellytag_bypass", lib_settings.get("jellytag_bypass", last_used.get("jellytag_bypass", False))))

		job_sort = (job.get("sort_order") or lib_settings.get("sort_order") or default_sort or "alphabetical").strip()
		if job_sort not in ("alphabetical", "recent"):
			job_sort = "alphabetical"

		norm_minres = {}
		for code, v in (job_minres or {}).items():
			try:
				if isinstance(v, (list, tuple)) and len(v) == 2:
					norm_minres[code] = [int(v[0]), int(v[1])]
			except Exception:
				pass

		if job.get("auto_html"):
			_run_generate_html_once(
				server=server,
				apikey=apikey,
				library=library,
				bgcolor=lib_settings.get("bgcolor", bgcolor),
				textcolor=lib_settings.get("textcolor", textcolor),
				tablebgcolor=lib_settings.get("tablebgcolor", tablebgcolor),
				images=job_images,
				minres=norm_minres,
				zipnames=job_zipnames,
				sort_order=job_sort,
				jellytag_bypass=job_jellytag_bypass,
			)

		if job.get("auto_zip"):
			_run_generate_zip_once(
				server=server,
				apikey=apikey,
				library=library,
				images=job_images,
				zipnames=job_zipnames,
				sort_order=job_sort,
				jellytag_bypass=job_jellytag_bypass,
			)

		try:
			_prune_outputs_for_library(
				library=library,
				keep_html=int(job.get("keep_html") or 0),
				keep_zip=int(job.get("keep_zip") or 0),
			)
		except Exception:
			pass


def _run_fresh_scan_all(server=None, library_ids=None):
	conn = _fresh_conn()
	server = server or _fresh_active_server(conn)
	if not server:
		try:
			conn.close()
		except Exception:
			pass
		return []
	results = []
	if library_ids is None and has_request_context() and request.is_json:
		library_ids = set((request.get_json(silent=True) or {}).get("library_ids") or [])
	elif library_ids is not None:
		library_ids = set(library_ids)
	libraries = _fresh_libraries(conn, server["id"])
	if library_ids:
		libraries = [library for library in libraries if library["id"] in library_ids]
	for library in libraries:
		try:
			result = scan_library(
				conn,
				server,
				library,
				global_thresholds=_fresh_global_thresholds(conn),
				jellytag_bypass=_fresh_jellytag_enabled(conn),
			)
			results.append({"library": library["name"], "status": "ok", **result})
		except Exception as e:
			app.logger.exception("Fresh scan failed for %s", library["name"])
			results.append({"library": library["name"], "status": "error", "message": str(e)})
	try:
		conn.close()
	except Exception:
		pass
	return results


def _fresh_scan_job_update(job_id, **values):
	with FRESH_SCAN_JOBS_LOCK:
		job = FRESH_SCAN_JOBS.get(job_id)
		if not job:
			return
		job.update(values)
		job["updated_at"] = fresh_state.utc_now()


def _fresh_start_scan_job(kind, server, library_id=None, item_id=None, library_ids=None):
	job_id = uuid.uuid4().hex
	with FRESH_SCAN_JOBS_LOCK:
		FRESH_SCAN_JOBS[job_id] = {
			"id": job_id,
			"kind": kind,
			"state": "queued",
			"library_id": library_id,
			"item_id": item_id,
			"created_at": fresh_state.utc_now(),
			"updated_at": fresh_state.utc_now(),
		}

	def runner():
		conn = None
		with app.app_context():
			try:
				_fresh_scan_job_update(job_id, state="running")
				conn = _fresh_conn()
				if kind == "library":
					library = conn.execute(
						"SELECT * FROM libraries WHERE server_id = ? AND id = ?",
						(server["id"], library_id),
					).fetchone()
					if not library:
						raise RuntimeError("Library not found.")
					result = scan_library(
						conn,
						server,
						library,
						global_thresholds=_fresh_global_thresholds(conn),
						jellytag_bypass=_fresh_jellytag_enabled(conn),
					)
				elif kind == "all":
					result = {"results": _run_fresh_scan_all(server, library_ids=library_ids or [])}
				elif kind == "item":
					library = conn.execute(
						"SELECT * FROM libraries WHERE server_id = ? AND id = ?",
						(server["id"], library_id),
					).fetchone()
					if not library:
						raise RuntimeError("Library not found.")
					result = scan_media_item(
						conn,
						server,
						library,
						item_id,
						global_thresholds=_fresh_global_thresholds(conn),
						jellytag_bypass=_fresh_jellytag_enabled(conn),
					)
				else:
					raise RuntimeError("Unknown scan job type.")
				_fresh_scan_job_update(job_id, state="done", result=result)
			except Exception as e:
				app.logger.exception("Fresh background scan failed")
				_fresh_scan_job_update(job_id, state="error", message=str(e))
			finally:
				try:
					if conn:
						conn.close()
				except Exception:
					pass

	thread = threading.Thread(target=runner, daemon=True)
	thread.start()
	return job_id


def _auto_scheduler_loop():
	app.logger.info("AUTO scheduler thread started")
	while True:
		try:
			auto = load_auto()
			expr = (auto.get("cron") or "").strip()
			if expr:
				now = now_in_tz()
				minute_key = now.strftime("%Y-%m-%d %H:%M")
				if cron_matches(now, expr) and auto.get("last_run_minute") != minute_key:
					app.logger.info("AUTO cron matched (%s); running jobs...", expr)
					_run_auto_sequence()
					auto["last_run_minute"] = minute_key
					save_auto(auto)
			scan_expr = (auto.get("fresh_scan_cron") or "").strip()
			if scan_expr:
				now = now_in_tz()
				minute_key = now.strftime("%Y-%m-%d %H:%M")
				if cron_matches(now, scan_expr) and auto.get("fresh_scan_last_run_minute") != minute_key:
					app.logger.info("FRESH AUTO scan cron matched (%s); scanning libraries...", scan_expr)
					_run_fresh_scan_all()
					auto["fresh_scan_last_run_minute"] = minute_key
					save_auto(auto)
		except Exception as e:
			app.logger.exception("AUTO scheduler error: %s", e)

		try:
			threading.Event().wait(30)
		except Exception:
			pass


# ----------------- Restore: server-side bulk mapping store -----------------
# NOTE: This is intentionally simple (single-user local tool). It only needs to
# persist long enough for the user to click "Apply All" right after "Accept All".
_LAST_BULK_MAPPINGS = {
	"library": "",
	"server": "",
	"apikey": "",
	"mappings": [],  # list of {folder, match}
	"updated_at": "",
}

_FRESH_RESTORE_CONTEXT = {}


def _fresh_restore_overrides_from_form(form):
	overrides = {}
	for key, value in form.items():
		if not key.startswith("restore_name_"):
			continue
		image_type = key.replace("restore_name_", "", 1).strip()
		name = (value or "").strip()
		if image_type and name:
			overrides[image_type] = name
	return overrides


def _fresh_restore_match_options(server, apikey, library):
	try:
		from restore import get_library_items
		items, _collection_type = get_library_items(server, apikey, library)
	except Exception:
		app.logger.warning("Unable to fetch Fresh restore match options", exc_info=True)
		return []
	options = []
	for item in items:
		name = (item.get("Name") or "").strip()
		if not name:
			continue
		year = item.get("ProductionYear") or item.get("Year") or ""
		display = f"{name} ({year})" if year and (item.get("Type") or "").lower() == "movie" else name
		options.append({"value": name, "display": display})
	return sorted(options, key=lambda row: row["display"].lower())


def _fresh_restore_library_selected_types(conn, target_server_id, library_name):
	row = None
	try:
		if target_server_id:
			row = conn.execute(
				"SELECT * FROM libraries WHERE server_id = ? AND name = ? COLLATE NOCASE",
				(int(target_server_id), library_name),
			).fetchone()
		if not row:
			server = _fresh_active_server(conn)
			if server:
				row = conn.execute(
					"SELECT * FROM libraries WHERE server_id = ? AND name = ? COLLATE NOCASE",
					(server["id"], library_name),
				).fetchone()
	except Exception:
		row = None
	if not row:
		return None
	return set(_fresh_selected_images(dict(row)))


def _fresh_restore_group_for_filename(filename, overrides):
	from restore import _infer_type, _season_number_from_name
	if _season_number_from_name(filename) is not None:
		return "Season Posters"
	return _infer_type(filename, overrides)


def _fresh_restore_image_groups(path, overrides, selected_codes):
	selected_labels = None
	if selected_codes is not None:
		selected_labels = {FRESH_IMAGE_TYPE_OPTIONS.get(code, code) for code in selected_codes}
	groups_by_folder = {}
	def add_file(folder, filename):
		ext = os.path.splitext(filename)[1].lower()
		if ext not in (".jpg", ".jpeg", ".png"):
			return
		group = _fresh_restore_group_for_filename(filename, overrides)
		if not group:
			return
		if group != "Season Posters" and selected_labels is not None and group not in selected_labels:
			return
		groups_by_folder.setdefault(folder, set()).add(group)
	if os.path.isfile(path) and path.lower().endswith(".zip"):
		with zipfile.ZipFile(path, "r") as zf:
			for name in zf.namelist():
				parts = name.replace("\\", "/").split("/")
				if len(parts) >= 2:
					add_file(parts[-2], parts[-1])
	else:
		base = path
		for folder in os.listdir(base) if os.path.isdir(base) else []:
			folder_path = os.path.join(base, folder)
			if not os.path.isdir(folder_path):
				continue
			for filename in os.listdir(folder_path):
				add_file(folder, filename)
	return {folder: sorted(groups) for folder, groups in groups_by_folder.items()}


def _fresh_restore_folder_files(path, folder):
	files = []
	try:
		if os.path.isfile(path) and path.lower().endswith(".zip"):
			with zipfile.ZipFile(path, "r") as zf:
				prefix = f"{folder}/".replace("\\", "/")
				for name in zf.namelist():
					if not name.replace("\\", "/").startswith(prefix):
						continue
					filename = name.replace("\\", "/").split("/")[-1]
					if os.path.splitext(filename)[1].lower() in (".jpg", ".jpeg", ".png"):
						files.append(filename)
		else:
			folder_path = os.path.join(path, folder)
			for filename in os.listdir(folder_path) if os.path.isdir(folder_path) else []:
				if os.path.splitext(filename)[1].lower() in (".jpg", ".jpeg", ".png"):
					files.append(filename)
	except Exception:
		files = []
	return sorted(set(files), key=str.lower)


def _fresh_restore_annotate_result(result, path, overrides, selected_codes):
	groups_by_folder = _fresh_restore_image_groups(path, overrides, selected_codes)
	for collection in ("matched", "below_threshold", "unmatched", "unmatched_folders"):
		for row in result.get(collection) or []:
			folder = row.get("folder") or ""
			row["image_groups"] = groups_by_folder.get(folder, [])
			row["images"] = _fresh_restore_folder_files(path, folder)
			row["restore_key"] = re.sub(r"[^A-Za-z0-9_.-]+", "_", folder) or "folder"
	for match in result.get("matches") or []:
		folder = match.get("folder") or ""
		match["image_groups"] = groups_by_folder.get(folder, [])
		if not match.get("images"):
			match["images"] = _fresh_restore_folder_files(path, folder)
	return result


def _fresh_restore_filters_from_form(form):
	included = []
	types_by_folder = {}
	for key, value in form.items():
		if not key.startswith("folder_"):
			continue
		row_key = key.replace("folder_", "", 1)
		folder = (value or "").strip()
		if not folder or form.get(f"include_{row_key}") != "on":
			continue
		included.append(folder)
		types_by_folder[folder] = form.getlist(f"types_{row_key}")
	return included, types_by_folder


@app.route("/restore_apply_bulk", methods=["POST"])
def restore_apply_bulk():
	"""
	Stores the user-approved mappings from the review screen (below-threshold table).
	Review.html will call this when "Accept All" is pressed.
	"""
	try:
		payload = request.get_json(silent=True) or {}
		mappings = payload.get("mappings") or []
		if not isinstance(mappings, list):
			mappings = []

		# Store latest
		_LAST_BULK_MAPPINGS["mappings"] = [
			{"folder": (m.get("folder") or "").strip(), "match": (m.get("match") or "").strip()}
			for m in mappings
			if isinstance(m, dict) and (m.get("folder") or "").strip() and (m.get("match") or "").strip()
		]
		_LAST_BULK_MAPPINGS["updated_at"] = now_in_tz().strftime("%Y-%m-%d %H:%M:%S")

		return Response(json.dumps({"status": "ok", "count": len(_LAST_BULK_MAPPINGS["mappings"])}),
						mimetype="application/json")
	except Exception as e:
		return Response(json.dumps({"status": "error", "message": str(e)}),
						mimetype="application/json", status=500)


@app.route("/restore_execute", methods=["GET"])
def restore_execute():
	"""
	Actually executes the restore after the user has reviewed + accepted mappings.
	Streams output in a new tab.

	Query params are provided by review.html:
	  library, server, apikey, dry_run, threshold, path
	"""
	try:
		library = (request.args.get("library") or "").strip()
		server = (request.args.get("server") or "").strip()
		apikey = (request.args.get("apikey") or "").strip()

		# dry_run may come through as "True"/"False" or "on"/"" etc.
		dry_raw = (request.args.get("dry_run") or "").strip().lower()
		dry_run = dry_raw in ("1", "true", "yes", "on")

		try:
			threshold = float(request.args.get("threshold") or 0.95)
		except Exception:
			threshold = 0.95

		path = request.args.get("path") or "output"

		# Build forced mapping dict from stored bulk mappings (if any)
		forced = {}
		for m in (_LAST_BULK_MAPPINGS.get("mappings") or []):
			folder = (m.get("folder") or "").strip()
			match = (m.get("match") or "").strip()
			if folder and match:
				forced[folder] = match

		def generate():
			yield _stream_page_open("Restore Executing")
			yield "🚀 Starting restore...\n"
			yield f"Library: {library}\n"
			yield f"Dry-run: {dry_run}\n"
			yield f"Threshold: {threshold}\n"
			yield f"Path: {path}\n"
			yield "\n"

			# Stream from restore engine
			os.environ["PIXELFIN_SERVER"] = server
			os.environ["PIXELFIN_APIKEY"] = apikey

			for line in run_restore_streamed(
				path=path,
				library=library,
				threshold=threshold,
				dry_run=dry_run,
				comparison_html=True,  # keep behavior; user already chose this upstream
				server=server,
				apikey=apikey,
				forced_mappings=forced,  # <-- requires restore.py update (you likely already did or will)
			):
				yield line

			yield "\n\n✅ Done.\n"
			yield "\n</pre>"
			yield "<div class='actions'>"
			yield f"<a class='btn' href='/?tab=restore-tab'>Back to Restore</a>"
			yield f"<a class='btn' href='/?tab=generate-tab'>Back to Main Menu</a>"
			yield "</div>"
			yield _stream_page_close()

		return Response(stream_with_context(generate()), mimetype="text/html")

	except Exception as e:
		app.logger.exception("restore_execute error")
		return Response(f"Error: {e}", status=500, mimetype="text/plain")


# ----------------- Routes -----------------
def _json_response(payload, status=200):
	return Response(json.dumps(payload), mimetype="application/json", status=status)


def _fresh_conn():
	return fresh_state.connect()


def _fresh_active_server(conn):
	row = conn.execute("SELECT * FROM servers WHERE is_active = 1 ORDER BY id LIMIT 1").fetchone()
	if row:
		return dict(row)
	row = conn.execute("SELECT * FROM servers ORDER BY id LIMIT 1").fetchone()
	if row:
		conn.execute("UPDATE servers SET is_active = CASE WHEN id = ? THEN 1 ELSE 0 END", (row["id"],))
		conn.commit()
		return dict(row)
	return None


def _fresh_global_thresholds(conn):
	return fresh_state.get_json(conn, "global_thresholds", DEFAULT_THRESHOLDS)


def _fresh_global_zipnames(conn):
	return fresh_state.get_json(conn, "global_zipnames", {})


def _fresh_jellytag_enabled(conn):
	return bool(fresh_state.get_json(conn, "jellytag_bypass", False))


def _fresh_layout(conn):
	layout = str(fresh_state.get_json(conn, "layout", "full") or "full").lower()
	return layout if layout in ("full", "compact") else "full"


def _fresh_servers(conn):
	return fresh_state.rows_to_dicts(conn.execute("SELECT * FROM servers ORDER BY is_active DESC, name COLLATE NOCASE").fetchall())


def _fresh_libraries(conn, server_id, include_hidden=False):
	sql = "SELECT * FROM libraries WHERE server_id = ?"
	params = [server_id]
	if not include_hidden:
		sql += " AND hidden = 0"
	sql += " ORDER BY name COLLATE NOCASE"
	libraries = [lib for lib in fresh_state.rows_to_dicts(conn.execute(sql, params).fetchall()) if is_supported_library(lib)]
	for library in libraries:
		lib_folder = os.path.join(BASE_OUTPUT_DIR, _safe_library_folder(library["name"]))
		try:
			library["zip_count"] = len([f for f in os.listdir(lib_folder) if f.lower().endswith(".zip")])
		except Exception:
			library["zip_count"] = 0
		task_count = 0
		items = fresh_state.rows_to_dicts(
			conn.execute("SELECT * FROM media_items WHERE server_id = ? AND library_id = ?", (server_id, library["id"])).fetchall()
		)
		for item in items:
			item["images"] = fresh_state.rows_to_dicts(
				conn.execute(
					"SELECT * FROM item_images WHERE server_id = ? AND item_id = ? ORDER BY code, label",
					(server_id, item["id"]),
				).fetchall()
			)
			if _fresh_apply_runtime_image_rules(conn, library, item).get("needs_attention"):
				task_count += 1
		library["task_count"] = task_count
	return libraries


def _fresh_runtime_thresholds(conn, library):
	thresholds = dict(_fresh_global_thresholds(conn) or {})
	try:
		thresholds.update(json.loads(library.get("thresholds") or "{}"))
	except Exception:
		pass
	return thresholds


def _fresh_selected_images(library):
	try:
		selected = json.loads(library.get("selected_images") or "[]")
	except Exception:
		selected = []
	return selected or list(DEFAULT_SELECTED_IMAGES)


def _fresh_apply_runtime_image_rules(conn, library, item):
	selected = set(_fresh_selected_images(library))
	thresholds = _fresh_runtime_thresholds(conn, library)
	images = [
		image for image in (item.get("images") or [])
		if not (image.get("code") == "sp" and str(image.get("label") or "").strip().lower() == "season posters")
	]
	existing_codes = {image.get("code") for image in images}
	for code in selected:
		if code == "sp":
			continue
		if code not in existing_codes and code in FRESH_IMAGE_TYPE_OPTIONS:
			images.append({
				"server_id": item.get("server_id"),
				"item_id": item.get("id"),
				"code": code,
				"label": FRESH_IMAGE_TYPE_OPTIONS.get(code, code),
				"url": "",
				"width": 0,
				"height": 0,
				"status": "missing",
				"is_low": 0,
				"is_missing": 1,
				"last_checked": item.get("last_scanned") or "",
			})
	needs_attention = False
	for image in images:
		is_missing = bool(image.get("is_missing") or image.get("status") == "missing")
		is_low = False
		if not is_missing:
			code = image.get("code")
			width = int(image.get("width") or 0)
			height = int(image.get("height") or 0)
			if code in thresholds:
				is_low = bool(check_low_res(code, width, height, {code: tuple(thresholds[code])}))
		image["is_missing"] = int(is_missing)
		image["is_low"] = int(is_low)
		image["status"] = "missing" if is_missing else ("low" if is_low else "ok")
		if image.get("code") in selected and (is_missing or is_low):
			needs_attention = True
	item["images"] = images
	item["needs_attention"] = int(needs_attention)
	item["selected_images"] = json.dumps(list(selected))
	_fresh_attach_image_urls(item)
	return item


def _fresh_cover_cache_key(server_id, library_id):
	raw = f"{server_id}_{library_id}"
	return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)


def _fresh_image_proxy_url(item_id, code, label):
	if not has_request_context():
		return f"/fresh/item-image/{quote(str(item_id), safe='')}/{quote(str(code), safe='')}/{quote(str(label or ''), safe='')}"
	return url_for("fresh_item_image", item_id=item_id, code=code, label=label or "")


def _fresh_attach_image_urls(item):
	for image in item.get("images") or []:
		if image.get("url") and not image.get("is_missing"):
			image["proxy_url"] = _fresh_image_proxy_url(item["id"], image["code"], image["label"])
	return item


def _fresh_sync_libraries(conn, server):
	views = list_views(server)
	now = fresh_state.utc_now()
	seen = set()
	for row in conn.execute("SELECT * FROM libraries WHERE server_id = ?", (server["id"],)).fetchall():
		if not is_supported_library(dict(row)):
			conn.execute("DELETE FROM libraries WHERE server_id = ? AND id = ?", (server["id"], row["id"]))
	for view in views:
		seen.add(view["id"])
		existing = conn.execute(
			"SELECT * FROM libraries WHERE server_id = ? AND id = ?",
			(server["id"], view["id"]),
		).fetchone()
		if existing:
			conn.execute(
				"UPDATE libraries SET name = ?, collection_type = ?, thumbnail_url = ? WHERE server_id = ? AND id = ?",
				(view["name"], view["collection_type"], view["thumbnail_url"], server["id"], view["id"]),
			)
		else:
			conn.execute(
				"""
				INSERT INTO libraries(server_id, id, name, collection_type, thumbnail_url, selected_images, thresholds, zipnames, last_scanned)
				VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
				""",
				(
					server["id"],
					view["id"],
					view["name"],
					view["collection_type"],
					view["thumbnail_url"],
					json.dumps(DEFAULT_SELECTED_IMAGES),
					json.dumps({}),
					json.dumps({}),
					"",
				),
			)
	conn.commit()
	return len(seen)


@app.route("/", methods=["GET"])
def fresh_index():
	conn = _fresh_conn()
	server = _fresh_active_server(conn)
	libraries = _fresh_libraries(conn, server["id"]) if server else []
	hidden_libraries = _fresh_libraries(conn, server["id"], include_hidden=True) if server else []
	total_tasks = sum(int(lib.get("task_count") or 0) for lib in libraries)
	return render_template(
		"fresh.html",
		servers=_fresh_servers(conn),
		active_server=server,
		libraries=libraries,
		all_libraries=hidden_libraries,
		total_tasks=total_tasks,
		image_types=FRESH_IMAGE_TYPE_OPTIONS,
		default_selected=DEFAULT_SELECTED_IMAGES,
		global_thresholds=_fresh_global_thresholds(conn),
		global_zipnames=_fresh_global_zipnames(conn),
		default_zip_basenames=FRESH_DEFAULT_ZIP_BASENAMES,
		jellytag_bypass=_fresh_jellytag_enabled(conn),
		layout=_fresh_layout(conn),
		auto=load_auto(),
		generated=list_generated_htmls(),
		active_page="fresh",
	)


@app.route("/fresh/api/servers", methods=["POST"])
def fresh_save_server():
	conn = _fresh_conn()
	payload = request.get_json(silent=True) or request.form.to_dict()
	server_id = payload.get("id")
	name = (payload.get("name") or payload.get("url") or "Jellyfin").strip()
	url = (payload.get("url") or "").strip().rstrip("/")
	api_key = (payload.get("api_key") or payload.get("apikey") or "").strip()
	if not url or not api_key:
		return _json_response({"status": "error", "message": "Server URL and API key are required."}, 400)
	now = fresh_state.utc_now()
	if server_id:
		conn.execute(
			"UPDATE servers SET name = ?, url = ?, api_key = ?, updated_at = ? WHERE id = ?",
			(name, url, api_key, now, server_id),
		)
	else:
		is_first = conn.execute("SELECT COUNT(*) AS c FROM servers").fetchone()["c"] == 0
		conn.execute(
			"INSERT INTO servers(name, url, api_key, is_active, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
			(name, url, api_key, int(is_first), now, now),
		)
	conn.commit()
	return _json_response({"status": "ok", "servers": _fresh_servers(conn)})


@app.route("/fresh/api/servers/<int:server_id>/activate", methods=["POST"])
def fresh_activate_server(server_id):
	conn = _fresh_conn()
	conn.execute("UPDATE servers SET is_active = CASE WHEN id = ? THEN 1 ELSE 0 END", (server_id,))
	conn.commit()
	return _json_response({"status": "ok"})


@app.route("/fresh/api/servers/<int:server_id>/test", methods=["POST"])
def fresh_test_server(server_id):
	conn = _fresh_conn()
	server = fresh_state.row_to_dict(conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone())
	if not server:
		return _json_response({"status": "error", "message": "Server not found."}, 404)
	try:
		info = test_server(server)
		conn.execute(
			"UPDATE servers SET last_checked = ?, last_status = ? WHERE id = ?",
			(fresh_state.utc_now(), "ok", server_id),
		)
		conn.commit()
		return _json_response({"status": "ok", "server_name": info.get("ServerName") or info.get("LocalAddress") or "Connected"})
	except Exception as e:
		conn.execute(
			"UPDATE servers SET last_checked = ?, last_status = ? WHERE id = ?",
			(fresh_state.utc_now(), str(e)[:200], server_id),
		)
		conn.commit()
		return _json_response({"status": "error", "message": str(e)}, 502)


@app.route("/fresh/api/sync-libraries", methods=["POST"])
def fresh_sync_libraries():
	conn = _fresh_conn()
	server = _fresh_active_server(conn)
	if not server:
		return _json_response({"status": "error", "message": "Add a Jellyfin server first."}, 400)
	try:
		count = _fresh_sync_libraries(conn, server)
		return _json_response({"status": "ok", "count": count})
	except Exception as e:
		return _json_response({"status": "error", "message": str(e)}, 502)


@app.route("/fresh/api/settings", methods=["POST"])
def fresh_save_settings():
	conn = _fresh_conn()
	payload = request.get_json(silent=True) or {}
	if "global_thresholds" in payload:
		fresh_state.set_json(conn, "global_thresholds", payload.get("global_thresholds") or {})
		if payload.get("apply_to_all"):
			server = _fresh_active_server(conn)
			if server:
				conn.execute(
					"UPDATE libraries SET thresholds = ? WHERE server_id = ?",
					(json.dumps(payload.get("global_thresholds") or {}), server["id"]),
				)
				conn.commit()
	if "global_zipnames" in payload:
		fresh_state.set_json(conn, "global_zipnames", payload.get("global_zipnames") or {})
		if payload.get("apply_to_all"):
			server = _fresh_active_server(conn)
			if server:
				conn.execute(
					"UPDATE libraries SET zipnames = ? WHERE server_id = ?",
					(json.dumps(payload.get("global_zipnames") or {}), server["id"]),
				)
				conn.commit()
	if "global_selected_images" in payload:
		selected = [c for c in payload.get("global_selected_images") or [] if c in FRESH_IMAGE_TYPE_OPTIONS]
		fresh_state.set_json(conn, "global_selected_images", selected)
		server = _fresh_active_server(conn)
		if server:
			conn.execute(
				"UPDATE libraries SET selected_images = ? WHERE server_id = ?",
				(json.dumps(selected), server["id"]),
			)
			conn.commit()
	if "fresh_auto" in payload:
		auto = load_auto()
		fresh_auto = payload.get("fresh_auto") or {}
		auto["cron"] = (fresh_auto.get("cron") or "").strip()
		auto["fresh_scan_cron"] = (fresh_auto.get("fresh_scan_cron") or "").strip()
		auto["fresh_global_zip"] = bool(fresh_auto.get("fresh_global_zip"))
		try:
			auto["fresh_keep_zip"] = int(fresh_auto.get("fresh_keep_zip") or 0)
		except Exception:
			auto["fresh_keep_zip"] = 0
		save_auto(auto)
	if "layout" in payload:
		layout = str(payload.get("layout") or "full").lower()
		fresh_state.set_json(conn, "layout", layout if layout in ("full", "compact") else "full")
	if "jellytag_bypass" in payload:
		fresh_state.set_json(conn, "jellytag_bypass", bool(payload.get("jellytag_bypass")))
	if "hidden_libraries" in payload:
		server = _fresh_active_server(conn)
		if server:
			hidden = set(payload.get("hidden_libraries") or [])
			for lib in _fresh_libraries(conn, server["id"], include_hidden=True):
				conn.execute(
					"UPDATE libraries SET hidden = ? WHERE server_id = ? AND id = ?",
					(1 if lib["id"] in hidden else 0, server["id"], lib["id"]),
				)
			conn.commit()
	return _json_response({"status": "ok"})


@app.route("/fresh/api/scan-all", methods=["POST"])
def fresh_scan_all():
	conn = _fresh_conn()
	server = _fresh_active_server(conn)
	if not server:
		return _json_response({"status": "error", "message": "No active server."}, 400)
	library_ids = []
	if request.is_json:
		library_ids = (request.get_json(silent=True) or {}).get("library_ids") or []
	job_id = _fresh_start_scan_job("all", server, library_ids=library_ids)
	return _json_response({"status": "ok", "job_id": job_id, "state": "queued"})


@app.route("/fresh/api/scan-jobs/<job_id>")
def fresh_scan_job_status(job_id):
	with FRESH_SCAN_JOBS_LOCK:
		job = dict(FRESH_SCAN_JOBS.get(job_id) or {})
	if not job:
		return _json_response({"status": "error", "message": "Scan job not found."}, 404)
	return _json_response({"status": "ok", "job": job})


@app.route("/fresh/api/libraries/<library_id>/settings", methods=["POST"])
def fresh_save_library_settings(library_id):
	conn = _fresh_conn()
	server = _fresh_active_server(conn)
	if not server:
		return _json_response({"status": "error", "message": "No active server."}, 400)
	payload = request.get_json(silent=True) or {}
	fields = []
	values = []
	if "selected_images" in payload:
		selected = [c for c in payload.get("selected_images") or [] if c in FRESH_IMAGE_TYPE_OPTIONS]
		fields.append("selected_images = ?")
		values.append(json.dumps(selected))
	if "thresholds" in payload:
		fields.append("thresholds = ?")
		values.append(json.dumps(payload.get("thresholds") or {}))
	if "zipnames" in payload:
		fields.append("zipnames = ?")
		values.append(json.dumps(payload.get("zipnames") or {}))
	if not fields:
		return _json_response({"status": "ok"})
	values.extend([server["id"], library_id])
	conn.execute(f"UPDATE libraries SET {', '.join(fields)} WHERE server_id = ? AND id = ?", values)
	conn.commit()
	library = fresh_state.row_to_dict(conn.execute("SELECT * FROM libraries WHERE server_id = ? AND id = ?", (server["id"], library_id)).fetchone())
	return _json_response({"status": "ok", "library": library})


@app.route("/fresh/api/libraries/<library_id>/scan", methods=["POST"])
def fresh_scan_library(library_id):
	conn = _fresh_conn()
	server = _fresh_active_server(conn)
	if not server:
		return _json_response({"status": "error", "message": "No active server."}, 400)
	library = conn.execute("SELECT * FROM libraries WHERE server_id = ? AND id = ?", (server["id"], library_id)).fetchone()
	if not library:
		return _json_response({"status": "error", "message": "Library not found."}, 404)
	if not is_supported_library(dict(library)):
		return _json_response({"status": "error", "message": "This library type is not supported by Pixelfin."}, 400)
	if library["hidden"]:
		return _json_response({"status": "error", "message": "Hidden libraries are paused."}, 400)
	try:
		job_id = _fresh_start_scan_job("library", server, library_id=library_id)
		return _json_response({"status": "ok", "job_id": job_id, "state": "queued"})
	except Exception as e:
		app.logger.exception("Fresh scan failed")
		return _json_response({"status": "error", "message": str(e)}, 502)


@app.route("/fresh/api/libraries/<library_id>")
def fresh_library_data(library_id):
	conn = _fresh_conn()
	server = _fresh_active_server(conn)
	if not server:
		return _json_response({"status": "error", "message": "No active server."}, 400)
	tasks_only = request.args.get("tasks") == "1"
	library = fresh_state.row_to_dict(conn.execute("SELECT * FROM libraries WHERE server_id = ? AND id = ?", (server["id"], library_id)).fetchone())
	if not library:
		return _json_response({"status": "error", "message": "Library not found."}, 404)
	items = fresh_state.rows_to_dicts(conn.execute(
		"SELECT * FROM media_items WHERE server_id = ? AND library_id = ? ORDER BY name COLLATE NOCASE",
		(server["id"], library_id),
	).fetchall())
	runtime_items = []
	for item in items:
		item["images"] = fresh_state.rows_to_dicts(
			conn.execute(
				"SELECT * FROM item_images WHERE server_id = ? AND item_id = ? ORDER BY code, label",
				(server["id"], item["id"]),
			).fetchall()
		)
		item = _fresh_apply_runtime_image_rules(conn, library, item)
		if not tasks_only or item.get("needs_attention"):
			runtime_items.append(item)
	return _json_response({"status": "ok", "library": library, "items": runtime_items})


@app.route("/fresh/api/libraries/<library_id>/items/<item_id>/scan", methods=["POST"])
def fresh_scan_media_item(library_id, item_id):
	conn = _fresh_conn()
	server = _fresh_active_server(conn)
	if not server:
		return _json_response({"status": "error", "message": "No active server."}, 400)
	library = conn.execute("SELECT * FROM libraries WHERE server_id = ? AND id = ?", (server["id"], library_id)).fetchone()
	if not library:
		return _json_response({"status": "error", "message": "Library not found."}, 404)
	try:
		job_id = _fresh_start_scan_job("item", server, library_id=library_id, item_id=item_id)
		return _json_response({"status": "ok", "job_id": job_id, "state": "queued"})
	except Exception as e:
		app.logger.exception("Fresh item scan failed")
		return _json_response({"status": "error", "message": str(e)}, 502)


@app.route("/fresh/api/tasks")
def fresh_all_tasks():
	conn = _fresh_conn()
	server = _fresh_active_server(conn)
	if not server:
		return _json_response({"status": "error", "message": "No active server."}, 400)
	items = fresh_state.rows_to_dicts(
		conn.execute(
			"""
			SELECT media_items.*, libraries.name AS library_name, libraries.selected_images AS selected_images, libraries.thresholds AS thresholds
			FROM media_items
			JOIN libraries ON libraries.server_id = media_items.server_id AND libraries.id = media_items.library_id
			WHERE media_items.server_id = ? AND libraries.hidden = 0
			ORDER BY libraries.name COLLATE NOCASE, media_items.name COLLATE NOCASE
			""",
			(server["id"],),
		).fetchall()
	)
	runtime_items = []
	for item in items:
		item["images"] = fresh_state.rows_to_dicts(
			conn.execute(
				"SELECT * FROM item_images WHERE server_id = ? AND item_id = ? ORDER BY code, label",
				(server["id"], item["id"]),
			).fetchall()
		)
		item = _fresh_apply_runtime_image_rules(conn, item, item)
		if item.get("needs_attention"):
			runtime_items.append(item)
	return _json_response({"status": "ok", "items": runtime_items})


@app.route("/fresh/library-cover/<library_id>")
def fresh_library_cover(library_id):
	conn = _fresh_conn()
	server = _fresh_active_server(conn)
	if not server:
		return Response(status=404)
	library = conn.execute(
		"SELECT * FROM libraries WHERE server_id = ? AND id = ?",
		(server["id"], library_id),
	).fetchone()
	if not library or not library["thumbnail_url"]:
		return Response(status=404)
	key = _fresh_cover_cache_key(server["id"], library_id)
	image_path = os.path.join(FRESH_COVER_CACHE_DIR, f"{key}.bin")
	meta_path = os.path.join(FRESH_COVER_CACHE_DIR, f"{key}.json")
	content_type = "image/jpeg"
	try:
		with open(meta_path, "r", encoding="utf-8") as fh:
			meta = json.load(fh)
	except Exception:
		meta = {}
	if os.path.exists(image_path) and meta.get("thumbnail_url") == library["thumbnail_url"]:
		content_type = meta.get("content_type") or content_type
		with open(image_path, "rb") as fh:
			return Response(fh.read(), mimetype=content_type, headers={"Cache-Control": "public, max-age=3600"})
	try:
		resp = requests.get(library["thumbnail_url"], timeout=(5, 20))
		resp.raise_for_status()
		content_type = resp.headers.get("Content-Type") or content_type
		with open(image_path, "wb") as fh:
			fh.write(resp.content)
		with open(meta_path, "w", encoding="utf-8") as fh:
			json.dump({"thumbnail_url": library["thumbnail_url"], "content_type": content_type}, fh)
		return Response(resp.content, mimetype=content_type, headers={"Cache-Control": "public, max-age=3600"})
	except Exception:
		app.logger.warning("Fresh cover cache fetch failed for %s", library_id, exc_info=True)
		if os.path.exists(image_path):
			with open(image_path, "rb") as fh:
				return Response(fh.read(), mimetype=content_type, headers={"Cache-Control": "public, max-age=3600"})
	return Response(status=404)


@app.route("/fresh/item-image/<item_id>/<code>/<path:label>")
def fresh_item_image(item_id, code, label):
	conn = _fresh_conn()
	server = _fresh_active_server(conn)
	if not server:
		return Response(status=404)
	image = conn.execute(
		"SELECT * FROM item_images WHERE server_id = ? AND item_id = ? AND code = ? AND label = ?",
		(server["id"], item_id, code, label),
	).fetchone()
	if not image or not image["url"]:
		return Response(status=404)
	try:
		resp = requests.get(image["url"], timeout=(5, 30))
		resp.raise_for_status()
		content_type = resp.headers.get("Content-Type") or "image/jpeg"
		return Response(
			resp.content,
			mimetype=content_type,
			headers={
				"Content-Disposition": "inline",
				"Cache-Control": "private, max-age=300",
			},
		)
	except Exception:
		app.logger.warning("Fresh image proxy failed for %s %s %s", item_id, code, label, exc_info=True)
		return Response(status=404)


def _fresh_library_export_settings(conn, server, library_id):
	library = conn.execute("SELECT * FROM libraries WHERE server_id = ? AND id = ?", (server["id"], library_id)).fetchone()
	if not library:
		raise RuntimeError("Library not found")
	images = json.loads(library["selected_images"] or "[]") or DEFAULT_SELECTED_IMAGES
	thresholds = _fresh_global_thresholds(conn)
	thresholds.update(json.loads(library["thresholds"] or "{}"))
	zipnames = dict(FRESH_DEFAULT_ZIP_BASENAMES)
	zipnames.update(_fresh_global_zipnames(conn))
	zipnames.update(json.loads(library["zipnames"] or "{}"))
	return dict(library), images, thresholds, zipnames


@app.route("/fresh/libraries/<library_id>/download-html", methods=["POST"])
def fresh_download_html(library_id):
	conn = _fresh_conn()
	server = _fresh_active_server(conn)
	if not server:
		return Response("No active server", status=400)
	try:
		library, images, thresholds, zipnames = _fresh_library_export_settings(conn, server, library_id)
		ok = _run_generate_html_once(
			server=server["url"],
			apikey=server["api_key"],
			library=library["name"],
			bgcolor="#101419",
			textcolor="#f5f7fb",
			tablebgcolor="#151b22",
			images=images,
			minres=thresholds,
			zipnames=zipnames,
			sort_order="alphabetical",
			jellytag_bypass=_fresh_jellytag_enabled(conn),
		)
		if not ok:
			return Response("HTML export failed", status=500)
		safe_library = _safe_library_folder(library["name"])
		filename = _newest_file_in_folder(os.path.join(BASE_OUTPUT_DIR, safe_library), exts=(".html",), exclude_prefixes=("restore-",))
		if request.headers.get("X-Requested-With") == "fetch":
			return _json_response({"status": "ok", "download_url": url_for("download_embedded", library=safe_library, filename=filename)})
		return redirect(url_for("download_embedded", library=safe_library, filename=filename))
	except Exception as e:
		return Response(str(e), status=500)


@app.route("/fresh/libraries/<library_id>/download-zip", methods=["POST"])
def fresh_download_zip(library_id):
	conn = _fresh_conn()
	server = _fresh_active_server(conn)
	if not server:
		return Response("No active server", status=400)
	try:
		library, images, _thresholds, zipnames = _fresh_library_export_settings(conn, server, library_id)
		jellytag_bypass = _fresh_jellytag_enabled(conn)
		app.logger.info("FRESH: ZIP export library=%s jellytag_bypass=%s", library["name"], jellytag_bypass)
		ok = _run_generate_zip_once(
			server=server["url"],
			apikey=server["api_key"],
			library=library["name"],
			images=images,
			zipnames=zipnames,
			sort_order="alphabetical",
			jellytag_bypass=jellytag_bypass,
		)
		if not ok:
			return Response("ZIP export failed", status=500)
		safe_library = _safe_library_folder(library["name"])
		auto = load_auto()
		try:
			keep_zip = int(auto.get("fresh_keep_zip") or 0)
		except Exception:
			keep_zip = 0
		if keep_zip > 0:
			_prune_outputs_for_library(library["name"], keep_html=0, keep_zip=keep_zip)
		filename = _newest_file_in_folder(os.path.join(BASE_OUTPUT_DIR, safe_library), exts=(".zip",))
		if request.headers.get("X-Requested-With") == "fetch":
			return _json_response({"status": "ok", "download_url": url_for("serve_output", library=safe_library, filename=filename)})
		return redirect(url_for("serve_output", library=safe_library, filename=filename))
	except Exception as e:
		return Response(str(e), status=500)


@app.route("/fresh/restore/review", methods=["POST"])
def fresh_restore_review():
	try:
		form = request.form.to_dict()
		conn = _fresh_conn()
		library = (form.get("library") or "").strip()
		threshold = float(form.get("threshold") or 0.75)
		dry_run = "dry_run" in request.form
		restore_mode = form.get("restore_mode", "pixelfin")
		server = form.get("server", "")
		apikey = form.get("apikey", "")
		overrides = _fresh_restore_overrides_from_form(form)
		if _fresh_jellytag_enabled(conn):
			overrides["__jellytag_bypass"] = True

		tmp_path = None
		if restore_mode == "device":
			file = request.files.get("zip_file")
			if file and file.filename:
				tmp_path = os.path.join("/tmp", file.filename)
				file.save(tmp_path)
		else:
			pixelfin_zip = form.get("pixelfin_zip", "")
			if pixelfin_zip:
				tmp_path = os.path.join("output", pixelfin_zip)

		if not tmp_path:
			return Response("Choose a ZIP file to restore.", status=400)

		selected_restore_types = _fresh_restore_library_selected_types(conn, form.get("target_server_id"), library)
		result = run_restore(
			path=tmp_path,
			library=library,
			threshold=threshold,
			dry_run=True,
			comparison_html=False,
			server=server,
			apikey=apikey,
			restore_filename_overrides=overrides,
		)
		result = _fresh_restore_annotate_result(result, tmp_path, overrides, selected_restore_types)
		match_options = _fresh_restore_match_options(server, apikey, library)

		_FRESH_RESTORE_CONTEXT.clear()
		_FRESH_RESTORE_CONTEXT.update({
			"path": tmp_path,
			"library": library,
			"threshold": threshold,
			"dry_run": dry_run,
			"server": server,
			"apikey": apikey,
			"restore_filename_overrides": overrides,
			"all_matches": result.get("all_matches") or [],
			"match_options": match_options,
			"selected_restore_types": list(selected_restore_types) if selected_restore_types is not None else None,
			"result": result,
		})

		return render_template(
			"fresh_restore.html",
			result=result,
			library=library,
			threshold=threshold,
			dry_run=dry_run,
			comparison_token="active",
			completed=False,
			pixelfin_favicon=PIXELFIN_FAVICON_BASE64,
			match_options=match_options,
			selected_restore_types=selected_restore_types,
		)
	except Exception as e:
		app.logger.exception("Fresh restore review failed")
		return Response(str(e), status=500)


@app.route("/fresh/restore/run", methods=["POST"])
def fresh_restore_run():
	if not _FRESH_RESTORE_CONTEXT:
		return Response("No restore review is active.", status=400)
	forced = {}
	for key, value in request.form.items():
		if key.startswith("map_") and value.strip():
			forced[key.replace("map_", "", 1)] = value.strip()
	try:
		included_folders, included_image_types_by_folder = _fresh_restore_filters_from_form(request.form)
		result = run_restore(
			path=_FRESH_RESTORE_CONTEXT["path"],
			library=_FRESH_RESTORE_CONTEXT["library"],
			threshold=float(_FRESH_RESTORE_CONTEXT["threshold"]),
			dry_run=False,
			comparison_html=False,
			server=_FRESH_RESTORE_CONTEXT["server"],
			apikey=_FRESH_RESTORE_CONTEXT["apikey"],
			forced_mappings=forced,
			restore_filename_overrides=_FRESH_RESTORE_CONTEXT.get("restore_filename_overrides") or {},
			included_folders=included_folders,
			included_image_types_by_folder=included_image_types_by_folder,
		)
		result = _fresh_restore_annotate_result(
			result,
			_FRESH_RESTORE_CONTEXT["path"],
			_FRESH_RESTORE_CONTEXT.get("restore_filename_overrides") or {},
			set(_FRESH_RESTORE_CONTEXT.get("selected_restore_types") or []) if _FRESH_RESTORE_CONTEXT.get("selected_restore_types") is not None else None,
		)
		_FRESH_RESTORE_CONTEXT["result"] = result
		return render_template(
			"fresh_restore.html",
			result=result,
			library=_FRESH_RESTORE_CONTEXT["library"],
			threshold=_FRESH_RESTORE_CONTEXT["threshold"],
			dry_run=False,
			comparison_token="active",
			completed=True,
			pixelfin_favicon=PIXELFIN_FAVICON_BASE64,
			match_options=_FRESH_RESTORE_CONTEXT.get("match_options") or [],
			selected_restore_types=set(_FRESH_RESTORE_CONTEXT.get("selected_restore_types") or []) if _FRESH_RESTORE_CONTEXT.get("selected_restore_types") is not None else None,
		)
	except Exception as e:
		app.logger.exception("Fresh restore run failed")
		return Response(str(e), status=500)


@app.route("/fresh/restore/preview/after/<token>/<path:folder>/<path:filename>")
def fresh_restore_preview_file(token, folder, filename):
	if token != "active" or not _FRESH_RESTORE_CONTEXT:
		return Response("Preview expired", status=404)
	path = _FRESH_RESTORE_CONTEXT.get("path") or ""
	arcname = f"{folder}/{filename}".replace("\\", "/")
	try:
		if os.path.isfile(path) and path.lower().endswith(".zip"):
			from zipfile import ZipFile
			with ZipFile(path, "r") as zf:
				data = zf.read(arcname)
			return Response(data, mimetype="image/jpeg")
		full = os.path.abspath(os.path.join(path, folder, filename))
		base = os.path.abspath(path)
		if not full.startswith(base + os.sep):
			return Response("Invalid path", status=400)
		return send_from_directory(os.path.dirname(full), os.path.basename(full))
	except Exception:
		return Response("Preview not found", status=404)


@app.route("/fresh/restore/preview/before/<token>/<int:match_index>/<int:before_index>")
def fresh_restore_preview_before(token, match_index, before_index):
	if token != "active" or not _FRESH_RESTORE_CONTEXT:
		return Response("Preview expired", status=404)
	try:
		result = _FRESH_RESTORE_CONTEXT.get("result") or {}
		match = (result.get("matches") or [])[match_index]
		before_path = (match.get("before_images") or [])[before_index]
		return send_from_directory(os.path.dirname(before_path), os.path.basename(before_path))
	except Exception:
		return Response("Preview not found", status=404)


@app.route("/fresh/restore/preview/current/<token>/<path:match>/<path:filename>")
def fresh_restore_preview_current(token, match, filename):
	if token != "active" or not _FRESH_RESTORE_CONTEXT:
		return Response("Preview expired", status=404)
	try:
		from restore import (
			SESSION,
			USER_AGENT,
			_DEFAULT_TIMEOUT,
			_get_season_items,
			_infer_type,
			_normalize_title,
			_season_number_from_name,
			get_library_items,
		)
		server = _FRESH_RESTORE_CONTEXT["server"]
		apikey = _FRESH_RESTORE_CONTEXT["apikey"]
		library = _FRESH_RESTORE_CONTEXT["library"]
		items, _collection_type = get_library_items(server, apikey, library)
		target = None
		for item in items:
			if _normalize_title(item.get("Name") or "") == _normalize_title(match):
				target = item
				break
		if not target:
			return Response("Preview not found", status=404)
		season_number = _season_number_from_name(filename)
		if season_number is not None:
			season_item = _get_season_items(server, apikey, target["Id"]).get(season_number)
			if not season_item:
				return Response("Preview not found", status=404)
			url = f"{server.rstrip('/')}/Items/{season_item['Id']}/Images/Primary"
		else:
			image_type = _infer_type(filename, _FRESH_RESTORE_CONTEXT.get("restore_filename_overrides") or {})
			if not image_type:
				return Response("Preview not found", status=404)
			url = f"{server.rstrip('/')}/Items/{target['Id']}/Images/{image_type}"
		if (_FRESH_RESTORE_CONTEXT.get("restore_filename_overrides") or {}).get("__jellytag_bypass"):
			url = generate_add_jellytag_bypass(url, True)
		response = SESSION.get(
			url,
			headers={"X-Emby-Token": apikey, "User-Agent": USER_AGENT},
			timeout=_DEFAULT_TIMEOUT,
		)
		if not response.ok or not response.content:
			return Response("Preview not found", status=404)
		content_type = response.headers.get("Content-Type") or "image/jpeg"
		return Response(response.content, mimetype=content_type)
	except Exception:
		app.logger.warning("Fresh current restore preview failed", exc_info=True)
		return Response("Preview not found", status=404)


@app.route("/classic", methods=["GET", "POST"])
def index():
	history = load_history()
	last_used = history.get("last_used", {})

	selected = {
		"server": last_used.get("server", ""),
		"library": "",
		"bgcolor": last_used.get("bgcolor", "#000000"),
		"textcolor": last_used.get("textcolor", "#ffffff"),
		"tablebgcolor": last_used.get("tablebgcolor", "#000000"),
		"images": last_used.get("images", list(IMAGE_TYPE_OPTIONS.keys())),
		"apikey": last_used.get("apikey", ""),
		"minres": last_used.get("minres", {}),
		"zipnames": last_used.get("zipnames", {}),
		"sort_order": last_used.get("sort_order", "alphabetical"),
		"jellytag_bypass": bool(last_used.get("jellytag_bypass", False)),
	}

	if request.method == "POST" or request.args.get("library"):
		server = request.form.get("server") or selected["server"]
		library = request.form.get("library") or request.args.get("library") or ""
		lib_settings = history.get("library_settings", {}).get(library, {})
		jellytag_bypass = (
			request.form.get("jellytag_bypass") == "on"
			if request.method == "POST"
			else bool(lib_settings.get("jellytag_bypass", last_used.get("jellytag_bypass", False)))
		)

		selected.update(
			{
				"server": server or lib_settings.get("server", ""),
				"library": library,
				"bgcolor": request.form.get("bgcolor", lib_settings.get("bgcolor", "#000000")),
				"textcolor": request.form.get("textcolor", lib_settings.get("textcolor", "#ffffff")),
				"tablebgcolor": request.form.get(
					"tablebgcolor", lib_settings.get("tablebgcolor", "#000000")
				),
				"images": request.form.getlist("images")
				or lib_settings.get("images", list(IMAGE_TYPE_OPTIONS.keys())),
				"apikey": request.form.get(
					"apikey", lib_settings.get("apikey", last_used.get("apikey", ""))
				),
				"minres": lib_settings.get("minres", last_used.get("minres", {})),
				"zipnames": lib_settings.get("zipnames", last_used.get("zipnames", {})),
				"sort_order": request.form.get(
					"sort_order",
					lib_settings.get("sort_order", last_used.get("sort_order", "alphabetical")),
				),
				"jellytag_bypass": jellytag_bypass,
			}
		)

	if request.method == "POST":
		action = request.form.get("action", "html")

		server = selected["server"]
		library = selected["library"]
		apikey = selected["apikey"]
		bgcolor = selected["bgcolor"]
		textcolor = selected["textcolor"]
		tablebgcolor = selected["tablebgcolor"]
		selected_images = selected["images"]
		jellytag_bypass = selected["jellytag_bypass"]

		# min resolution
		minres = {}
		for code in IMAGE_TYPE_OPTIONS:
			try:
				w = int(request.form.get(f"minres_{code}_w") or 0)
				h = int(request.form.get(f"minres_{code}_h") or 0)
				if w > 0 and h > 0:
					minres[code] = (w, h)
			except ValueError:
				continue

		# zip filename overrides
		zipnames = {}
		for code in IMAGE_TYPE_OPTIONS:
			val = (request.form.get(f"zipname_{code}", "") or "").strip()
			if val:
				zipnames[code] = val

		sort_order = request.form.get("sort_order", "alphabetical")

		save_history(
			server,
			library,
			{
				"apikey": apikey,
				"bgcolor": bgcolor,
				"textcolor": textcolor,
				"tablebgcolor": tablebgcolor,
				"images": selected_images,
				"minres": minres,
				"zipnames": zipnames,
				"sort_order": sort_order,
				"jellytag_bypass": jellytag_bypass,
			},
		)

		safe_library = _safe_library_folder(library)
		lib_folder = os.path.join(BASE_OUTPUT_DIR, safe_library)
		os.makedirs(lib_folder, exist_ok=True)

		now = now_in_tz()
		timestamp_file = now.strftime("%Y-%m-%d_%H-%M-%S")
		timestamp_html = now.strftime("%Y-%m-%d %H:%M:%S")

		log_queue = queue.Queue()

		# ---------- ZIP ----------
		if action == "zip":
			zip_filename = f"{timestamp_file} - {library}.zip"
			zip_path = os.path.join(lib_folder, zip_filename)

			def run_create_zip():
				args = [
					"python",
					"generate_html.py",
					"--server",
					server,
					"--apikey",
					apikey,
					"--library",
					library,
					"--images",
					",".join(selected_images),
					"--zip-output",
					zip_path,
					"--zipnames",
					json.dumps(zipnames),
					"--sort",
					sort_order,
				]
				if jellytag_bypass:
					args.append("--jellytag-bypass")
				proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
				for line in proc.stdout:
					log_queue.put(line)
				proc.wait()
				log_queue.put(None)

			threading.Thread(target=run_create_zip).start()

			def generate_zip_stream():
				yield _stream_page_open("Creating ZIP")

				while True:
					line = log_queue.get()
					if line is None:
						break
					yield line

				yield "\n</pre>"

				newest_zip = _newest_file_in_folder(lib_folder, exts=(".zip",))
				zip_filename_real = newest_zip or zip_filename
				zip_url = url_for("serve_output", library=safe_library, filename=zip_filename_real)

				yield "<div class='actions'>"
				yield f"<a class='btn' href='{zip_url}'>Download ZIP</a>"
				yield f"<a class='btn' href='/?tab=generate-tab'>Back to Main Menu</a>"
				yield "</div>"
				yield _stream_page_close()

			return Response(stream_with_context(generate_zip_stream()), mimetype="text/html")

		# ---------- HTML ----------
		sort_suffix = "Alphabetical" if sort_order == "alphabetical" else "Date-Added"
		html_filename = f"{timestamp_file} - {library} - {sort_suffix}.html"
		output_file = os.path.join(lib_folder, html_filename)

		def run_generate_html():
			args = [
				"python",
				"generate_html.py",
				"--server",
				server,
				"--apikey",
				apikey,
				"--library",
				library,
				"--output",
				output_file,
				"--bgcolor",
				bgcolor,
				"--textcolor",
				textcolor,
				"--tablebgcolor",
				tablebgcolor,
				"--images",
				",".join(selected_images),
				"--timestamp",
				timestamp_html,
				"--sort",
				sort_order,
			]
			if jellytag_bypass:
				args.append("--jellytag-bypass")
			if minres:
				minres_str = ";".join([f"{code}:{w}x{h}" for code, (w, h) in minres.items()])
				args += ["--minres", minres_str]

			proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
			for line in proc.stdout:
				log_queue.put(line)
			proc.wait()
			log_queue.put(None)

		threading.Thread(target=run_generate_html).start()

		def generate_html_stream():
			yield _stream_page_open("HTML Generated")

			while True:
				line = log_queue.get()
				if line is None:
					break
				yield line

			yield "\n</pre>"

			newest_html = _newest_file_in_folder(
				lib_folder, exts=(".html",), exclude_prefixes=("restore-",)
			)
			html_filename_real = newest_html or html_filename
			output_file_real = os.path.join(lib_folder, html_filename_real)

			# Inject favicon only (avoid duplicate Pixelfin logo; generate_html already shows it)
			try:
				if os.path.exists(output_file_real):
					with open(output_file_real, "r", encoding="utf-8") as f:
						content = f.read()

					if "data:image/png;base64," not in content and "<link rel='icon'" not in content:
						content = content.replace(
							"<head>",
							f"<head>\n<link rel='icon' type='image/png' href='data:image/png;base64,{PIXELFIN_FAVICON_BASE64}' />",
							1,
						)
						with open(output_file_real, "w", encoding="utf-8") as f:
							f.write(content)
			except Exception as e:
				yield f"<div class='muted'>⚠️ Pixelfin favicon injection failed: {str(e)}</div>"

			view_url = url_for("serve_output", library=safe_library, filename=html_filename_real)
			download_url = url_for("download_embedded", library=safe_library, filename=html_filename_real)

			yield "<div class='actions'>"
			yield f"<a class='btn' href='{view_url}' target='_blank'>View</a>"
			yield f"<a class='btn' href='{download_url}'>Download (embedded)</a>"
			yield f"<a class='btn' href='/?tab=generate-tab'>Back to Main Menu</a>"
			yield "</div>"

			yield _stream_page_close()

		return Response(stream_with_context(generate_html_stream()), mimetype="text/html")

	return render_template(
		"form.html",
		image_types=IMAGE_TYPE_OPTIONS,
		generated=list_generated_htmls(),
		history=history,
		selected=selected,
		pixelfin=PIXELFIN_BASE64,
		default_zip_basenames=DEFAULT_ZIP_BASENAMES,
		available_zips=list_zip_files(),  # ✅ now sorted per your rule
		active_page="main",
	)


# ----------------- Auto Page -----------------
@app.route("/auto", methods=["GET", "POST"])
def auto_page():
	history = load_history()
	auto = load_auto()

	if request.method == "POST":
		cron = (request.form.get("cron") or "").strip()

		# Gather indices that exist (handles gaps when rows are deleted)
		idxs = set()
		for k in request.form.keys():
			m = re.match(r"^job_(\d+)_library$", k)
			if m:
				idxs.add(int(m.group(1)))

		jobs = []
		for i in sorted(idxs):
			lib = (request.form.get(f"job_{i}_library") or "").strip()
			if not lib:
				continue

			auto_html = request.form.get(f"job_{i}_auto_html") == "on"
			auto_zip = request.form.get(f"job_{i}_auto_zip") == "on"

			try:
				keep_html = int(request.form.get(f"job_{i}_keep_html") or 0)
			except Exception:
				keep_html = 0
			try:
				keep_zip = int(request.form.get(f"job_{i}_keep_zip") or 0)
			except Exception:
				keep_zip = 0

			images = request.form.getlist(f"job_{i}_images")
			if not images:
				images = list(IMAGE_TYPE_OPTIONS.keys())

			minres = {}
			for code in IMAGE_TYPE_OPTIONS.keys():
				try:
					w = int(request.form.get(f"job_{i}_minres_{code}_w") or 0)
					h = int(request.form.get(f"job_{i}_minres_{code}_h") or 0)
					if w > 0 and h > 0:
						minres[code] = [w, h]
				except Exception:
					pass

			zipnames = {}
			for code in IMAGE_TYPE_OPTIONS.keys():
				val = (request.form.get(f"job_{i}_zipname_{code}") or "").strip()
				if val:
					zipnames[code] = val

			sort_order = (request.form.get(f"job_{i}_sort_order") or "alphabetical").strip()
			if sort_order not in ("alphabetical", "recent"):
				sort_order = "alphabetical"

			jobs.append({
				"library": lib,
				"auto_html": auto_html,
				"keep_html": keep_html,
				"auto_zip": auto_zip,
				"keep_zip": keep_zip,
				"images": images,
				"minres": minres,
				"zipnames": zipnames,
				"sort_order": sort_order,
			})

		# preserve last_run_minute
		payload = load_auto()
		payload["cron"] = cron
		payload["jobs"] = jobs
		save_auto(payload)
		return redirect(url_for("auto_page"))

	return render_template(
		"auto.html",
		history=history,
		image_types=IMAGE_TYPE_OPTIONS,
		default_zip_basenames=DEFAULT_ZIP_BASENAMES,
		pixelfin=PIXELFIN_BASE64,
		auto=auto,
		active_page="auto",
	)


# ----------------- Keep Toggle Route (NEW) -----------------
@app.route("/toggle_keep/<library>/<filename>")
def toggle_keep(library, filename):
	"""
	library is the output folder (already safe, ex: Movies or Movies_4K)
	filename is the file in that folder
	"""
	tab = request.args.get("tab", "generate-tab")

	# only allow toggling for existing output files
	file_path = os.path.join(BASE_OUTPUT_DIR, library, filename)
	if not os.path.exists(file_path):
		# if it doesn't exist, also remove stale keep entry if any
		data = load_keep()
		try:
			if library in data.get("kept", {}) and filename in data["kept"][library]:
				del data["kept"][library][filename]
				if not data["kept"][library]:
					del data["kept"][library]
				save_keep(data)
		except Exception:
			pass
		return redirect(url_for("index") + f"?tab={tab}")

	toggle_keep_file(library, filename)
	return redirect(url_for("index") + f"?tab={tab}")


@app.route("/restore", methods=["POST"])
def restore_images():
	try:
		form = request.form.to_dict()
		library = (form.get("library", "") or "").strip()
		threshold = float(form.get("threshold", 0.95))
		dry_run = "dry_run" in form
		comparison_html = "comparison_html" in form
		restore_mode = form.get("restore_mode", "pixelfin")
		pixelfin_zip = form.get("pixelfin_zip", "")
		server = form.get("server", "")
		apikey = form.get("apikey", "")

		restore_behavior = "semiauto"
		tmp_path = None

		if restore_mode == "device":
			file = request.files.get("zip_file")
			if file and file.filename:
				tmp_path = os.path.join("/tmp", file.filename)
				file.save(tmp_path)
		elif restore_mode == "pixelfin" and pixelfin_zip:
			tmp_path = os.path.join("output", pixelfin_zip)

		os.environ["PIXELFIN_SERVER"] = server
		os.environ["PIXELFIN_APIKEY"] = apikey

		# IMPORTANT:
		# /restore is ALWAYS a review step. We run the engine in dry-run mode for safety
		# to compute matched + unmatched + below-threshold. The actual restore happens in /restore_execute.
		review_dry = True

		if restore_behavior in ["semi", "semiauto"]:
			from restore import run_restore, get_library_items

			raw_result = run_restore(
				path=tmp_path or "output",
				library=library,
				threshold=threshold,
				dry_run=review_dry,                 # <-- always dry-run for review
				comparison_html=comparison_html,    # generates embedded HTML report
				server=server,
				apikey=apikey,
			)

			matched = []
			for entry in raw_result.get("matches", []):
				folder_name = entry.get("folder") or entry.get("name") or "Unknown"
				best_match = entry.get("match") or entry.get("best_match") or "—"
				try:
					sim = float(entry.get("similarity", entry.get("score", 0)))
				except Exception:
					sim = 0.0
				if sim <= 1.0:
					sim *= 100.0

				matched.append({"folder": folder_name, "best_match": best_match, "similarity": round(sim, 2)})

			# IMPORTANT: use the already-paged restore result instead of re-calling get_library_items incorrectly.
			# get_library_items() returns (items, collection_type), so iterating it directly can truncate/break the dropdown.
			all_items = raw_result.get("all_matches") or []
			if not all_items:
				try:
					items, _collection_type = get_library_items(server, apikey, library)
					all_items = sorted({(m.get("Name") or "").strip() for m in items if (m.get("Name") or "").strip()}, key=str.lower)
				except Exception:
					all_items = []

			result = {
				"matched": sorted(raw_result.get("matched") or matched, key=lambda x: x["folder"].lower()),
				"unmatched": sorted(raw_result.get("unmatched") or [], key=lambda x: x["folder"].lower()),
				"below_threshold": sorted(raw_result.get("below_threshold") or [], key=lambda x: x["folder"].lower()),
				"unmatched_folders": sorted(raw_result.get("unmatched_folders") or [], key=lambda x: x["folder"].lower()),
				"all_matches": sorted(all_items, key=str.lower),
				"comparison_html": raw_result.get("comparison_html"),
			}

			html_link = None
			if result["comparison_html"]:
				safe_lib = _safe_library_folder(library)
				html_link = f"/output/{quote(safe_lib)}/{os.path.basename(result['comparison_html'])}"

			zip_path = tmp_path if (tmp_path and tmp_path.endswith(".zip")) else "output"

			# Remember last restore context (so /restore_execute can succeed even if UI changes later)
			_LAST_BULK_MAPPINGS["library"] = library
			_LAST_BULK_MAPPINGS["server"] = server
			_LAST_BULK_MAPPINGS["apikey"] = apikey

			return render_template(
				"review.html",
				result=result,
				library=library,
				server=server,
				apikey=apikey,
				html_link=html_link,
				dry_run=dry_run,  # <-- user intent (apply step will respect this)
				pixelfin_logo=PIXELFIN_BASE64,
				pixelfin_favicon=PIXELFIN_FAVICON_BASE64,
				threshold=threshold,
				zip_path=zip_path,
			)

	except Exception as e:
		app.logger.exception("Error during restore")
		return Response(
			json.dumps({"status": "error", "message": str(e)}),
			mimetype="application/json",
			status=500,
		)


@app.route("/output/<library>/<filename>")
def serve_output(library, filename):
	return send_from_directory(os.path.join(BASE_OUTPUT_DIR, library), filename)


@app.route("/delete/<library>/<filename>")
def delete_file(library, filename):
	tab = request.args.get("tab", "generate-tab")

	file_path = os.path.join(BASE_OUTPUT_DIR, library, filename)
	if os.path.exists(file_path):
		os.remove(file_path)

	# ✅ also remove from keep map if present
	data = load_keep()
	try:
		if library in data.get("kept", {}) and filename in data["kept"][library]:
			del data["kept"][library][filename]
			if not data["kept"][library]:
				del data["kept"][library]
			save_keep(data)
	except Exception:
		pass

	lib_folder = os.path.join(BASE_OUTPUT_DIR, library)
	if os.path.exists(lib_folder) and not os.listdir(lib_folder):
		os.rmdir(lib_folder)

	return redirect(url_for("index") + f"?tab={tab}")


@app.route("/download/<library>/<filename>")
def download_embedded(library, filename):
	file_path = os.path.join(BASE_OUTPUT_DIR, library, filename)
	if not os.path.exists(file_path):
		return "File not found", 404

	with open(file_path, "r", encoding="utf-8") as f:
		html = f.read()
	try:
		conn = _fresh_conn()
		jellytag_bypass = _fresh_jellytag_enabled(conn)
	except Exception:
		jellytag_bypass = False

	def embed_img(match):
		full_tag = match.group(0)
		url = generate_add_jellytag_bypass(match.group(1), jellytag_bypass)

		try:
			# Leave already-embedded images alone
			if url.startswith("data:"):
				return full_tag

			resp = requests.get(url, timeout=30)
			resp.raise_for_status()
			img_data = base64.b64encode(resp.content).decode("utf-8")

			content_type = (resp.headers.get("Content-Type") or "").lower()
			if "jpeg" in content_type or "jpg" in content_type:
				mime = "image/jpeg"
			elif "png" in content_type:
				mime = "image/png"
			elif "gif" in content_type:
				mime = "image/gif"
			elif "webp" in content_type:
				mime = "image/webp"
			elif "bmp" in content_type:
				mime = "image/bmp"
			else:
				ext = url.split(".")[-1].split("?")[0].lower()
				if ext in ["jpg", "jpeg"]:
					mime = "image/jpeg"
				elif ext == "png":
					mime = "image/png"
				elif ext == "gif":
					mime = "image/gif"
				elif ext == "webp":
					mime = "image/webp"
				elif ext == "bmp":
					mime = "image/bmp"
				else:
					mime = "image/png"

			# Rebuild the <img> tag cleanly for downloaded HTML.
			# This avoids leaving behind broken attribute text when stripping lightbox behavior.
			attrs = []

			alt_match = re.search(r'alt="([^"]*)"', full_tag, flags=re.IGNORECASE)
			class_match = re.search(r'class="([^"]*)"', full_tag, flags=re.IGNORECASE)
			loading_match = re.search(r'loading="([^"]*)"', full_tag, flags=re.IGNORECASE)
			style_match = re.search(r'style="([^"]*)"', full_tag, flags=re.IGNORECASE)
			id_match = re.search(r'id="([^"]*)"', full_tag, flags=re.IGNORECASE)

			if id_match:
				attrs.append(f'id="{id_match.group(1)}"')
			attrs.append(f'src="data:{mime};base64,{img_data}"')
			if class_match:
				attrs.append(f'class="{class_match.group(1)}"')
			if alt_match:
				attrs.append(f'alt="{alt_match.group(1)}"')
			if loading_match:
				attrs.append(f'loading="{loading_match.group(1)}"')
			if style_match:
				attrs.append(f'style="{style_match.group(1)}"')

			self_closing = full_tag.rstrip().endswith("/>")
			return f"<img {' '.join(attrs)}{' /' if self_closing else ''}>"

		except Exception as e:
			app.logger.error(f"Failed to embed image {url}: {e}")
			return full_tag

	html_embedded = re.sub(
		r'<img\b[^>]*\bsrc="([^"]+)"[^>]*>',
		embed_img,
		html,
		flags=re.IGNORECASE,
	)

	return Response(
		html_embedded,
		mimetype="text/html",
		headers={"Content-Disposition": f"attachment; filename={filename}"},
	)


@app.route("/assets/<filename>")
def serve_assets(filename):
	return send_from_directory(ASSETS_DIR, filename)


# start scheduler thread once
_SCHED_STARTED = False


@app.before_request
def _ensure_scheduler_started():
	global _SCHED_STARTED
	if not _SCHED_STARTED:
		t = threading.Thread(target=_auto_scheduler_loop, daemon=True)
		t.start()
		_SCHED_STARTED = True


if __name__ == "__main__":
	# if running directly, start scheduler too
	t = threading.Thread(target=_auto_scheduler_loop, daemon=True)
	t.start()
	app.run(host="0.0.0.0", port=1280)
