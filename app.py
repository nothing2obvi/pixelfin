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
from urllib.parse import quote
from restore import run_restore_streamed

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

os.makedirs("data", exist_ok=True)
os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)

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
	tzname = os.environ.get("TZ")
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


def _run_generate_html_once(server, apikey, library, bgcolor, textcolor, tablebgcolor, images, minres, zipnames, sort_order):
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

	app.logger.info("AUTO: running HTML for library=%s sort=%s", library, sort_order)
	proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

	if proc.returncode != 0:
		app.logger.error("AUTO: HTML failed for %s\n%s", library, proc.stdout or "")
	return proc.returncode == 0


def _run_generate_zip_once(server, apikey, library, images, zipnames, sort_order):
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

	app.logger.info("AUTO: running ZIP for library=%s sort=%s", library, sort_order)
	proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
	if proc.returncode != 0:
		app.logger.error("AUTO: ZIP failed for %s\n%s", library, proc.stdout or "")
	return proc.returncode == 0


def _run_auto_sequence():
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
			)

		if job.get("auto_zip"):
			_run_generate_zip_once(
				server=server,
				apikey=apikey,
				library=library,
				images=job_images,
				zipnames=job_zipnames,
				sort_order=job_sort,
			)

		try:
			_prune_outputs_for_library(
				library=library,
				keep_html=int(job.get("keep_html") or 0),
				keep_zip=int(job.get("keep_zip") or 0),
			)
		except Exception:
			pass


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
@app.route("/", methods=["GET", "POST"])
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
	}

	if request.method == "POST" or request.args.get("library"):
		server = request.form.get("server") or selected["server"]
		library = request.form.get("library") or request.args.get("library") or ""
		lib_settings = history.get("library_settings", {}).get(library, {})

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

	def embed_img(match):
		full_tag = match.group(0)
		url = match.group(1)

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

def start_scheduler_once():
	global _SCHED_STARTED
	if not _SCHED_STARTED:
		t = threading.Thread(target=_auto_scheduler_loop, daemon=True)
		t.start()
		_SCHED_STARTED = True
		
start_scheduler_once()
		
if __name__ == "__main__":
	app.run(host="0.0.0.0", port=1280)