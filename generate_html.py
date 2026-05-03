import requests
import argparse
import sys
import os
import warnings
import tempfile
import json
import base64
import shutil
from urllib.parse import urljoin
from PIL import Image, ImageFile
from datetime import datetime, timezone
from typing import Dict, Tuple, Generator, List, Optional
from zipfile import ZipFile, ZIP_DEFLATED
import re

# Keep original warning suppression behavior
warnings.simplefilter("ignore", Image.DecompressionBombWarning)

# Reuse parser to avoid full image load
ImageFile.LOAD_TRUNCATED_IMAGES = True

# Base directory of this script (used to resolve relative output paths safely)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_pixelfin_logo_base64() -> str:
	"""
	Loads /app/assets/Pixelfin.png (resolved relative to this script) and returns base64 string.
	Returns "" if missing.
	"""
	try:
		logo_path = os.path.join(BASE_DIR, "assets", "Pixelfin.png")
		if os.path.exists(logo_path):
			with open(logo_path, "rb") as f:
				return base64.b64encode(f.read()).decode("utf-8")
	except Exception:
		pass
	return ""


PIXELFIN_LOGO_BASE64 = _load_pixelfin_logo_base64()

# ----------------------------------------------------------------------
# CONSTANTS
# ----------------------------------------------------------------------

IMAGE_TYPES_MAP = {
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
}

IMAGE_TYPES_REVERSE = {v: k for k, v in IMAGE_TYPES_MAP.items()}

LEFT_TYPES = ["p", "t", "c", "m"]
RIGHT_TYPES = ["bd", "bn", "b", "br", "d", "l"]

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
}

_DEFAULT_TIMEOUT = (10, 120)
_session: Optional[requests.Session] = None
_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n]+')


# ----------------------------------------------------------------------
# GENERAL HELPERS
# ----------------------------------------------------------------------
def safe_library_name(name: str) -> str:
	return re.sub(r"[^A-Za-z0-9_\-]", "_", name or "Unknown")


def sanitize_folder_name(name: str) -> str:
	s = _SAFE_NAME_RE.sub("_", name or "")
	s = s.strip().strip(".")
	return s or "Untitled"


def _safe_name(item: dict) -> str:
	return str(item.get("Name", "") or "")


def _get_session() -> requests.Session:
	global _session
	if _session is None:
		_session = requests.Session()
		_session.headers.update({"User-Agent": "generate_html.py (memory-friendly)"})
	return _session


def parse_minres_arg(minres_str):
	result = {}
	if not minres_str:
		return result
	parts = [p.strip() for p in minres_str.split(";") if p.strip()]
	for part in parts:
		try:
			code, wh = part.split(":", 1)
			w, h = wh.lower().split("x", 1)
			w = int(w)
			h = int(h)
			if code in IMAGE_TYPES_MAP and w > 0 and h > 0:
				result[code] = (w, h)
		except Exception:
			continue
	return result


def check_low_res(code, width, height, minres):
	if not code or code not in minres:
		return False
	min_w, min_h = minres[code]
	return (width and height) and (width < min_w or height < min_h)


def extract_year(item: dict) -> Optional[str]:
	if item.get("ProductionYear"):
		return str(item["ProductionYear"])
	premiere = item.get("PremiereDate")
	if premiere:
		try:
			return str(datetime.fromisoformat(str(premiere).replace("Z", "+00:00")).year)
		except Exception:
			pass
	return None


def build_item_display_name_map(items: List[dict], library_type: str) -> Dict[str, str]:
	"""
	Build one stable, collision-safe display/folder name per item ID.

	Movies:
	  Title (Year)
	  If duplicate even after year, append " 2", " 3", etc.

	Other libraries:
	  Title
	  If duplicate and a year exists, use Title (Year)
	  If still duplicate, append " 2", " 3", etc.
	"""
	name_map: Dict[str, str] = {}
	is_movies = (library_type or "").lower() == "movies"

	primary_groups: Dict[str, List[dict]] = {}

	for item in items:
		item_id = item.get("Id")
		if not item_id:
			continue

		title = sanitize_folder_name(_safe_name(item))
		year = extract_year(item)

		if is_movies:
			base = f"{title} ({year})" if year else title
		else:
			base = title

		primary_groups.setdefault(base, []).append(item)

	for base, grouped_items in primary_groups.items():
		if len(grouped_items) == 1:
			item_id = grouped_items[0].get("Id")
			if item_id:
				name_map[item_id] = base
			continue

		if not is_movies:
			refined_groups: Dict[str, List[dict]] = {}
			for item in grouped_items:
				item_id = item.get("Id")
				if not item_id:
					continue

				title = sanitize_folder_name(_safe_name(item))
				year = extract_year(item)
				refined = f"{title} ({year})" if year else title
				refined_groups.setdefault(refined, []).append(item)

			for refined, refined_items in refined_groups.items():
				if len(refined_items) == 1:
					item_id = refined_items[0].get("Id")
					if item_id:
						name_map[item_id] = refined
				else:
					for idx, item in enumerate(refined_items, start=1):
						item_id = item.get("Id")
						if item_id:
							name_map[item_id] = refined if idx == 1 else f"{refined} {idx}"
			continue

		for idx, item in enumerate(grouped_items, start=1):
			item_id = item.get("Id")
			if item_id:
				name_map[item_id] = base if idx == 1 else f"{base} {idx}"

	return name_map


def pick_extension(url: str, content_type: Optional[str]) -> str:
	if content_type:
		ct = content_type.lower()
		if "jpeg" in ct or "jpg" in ct:
			return ".jpg"
		if "png" in ct:
			return ".png"
		if "webp" in ct:
			return ".webp"
		if "gif" in ct:
			return ".gif"
		if "bmp" in ct:
			return ".bmp"

	ext = os.path.splitext(url.split("?", 1)[0])[1].lower()
	if ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"]:
		return ".jpg" if ext == ".jpeg" else ext
	return ".jpg"


def stream_to_bytes(url: str) -> tuple[bytes, str]:
	resp = _get_session().get(url, stream=True, timeout=_DEFAULT_TIMEOUT)
	resp.raise_for_status()
	content_type = resp.headers.get("Content-Type", "")
	chunks = []
	for chunk in resp.iter_content(chunk_size=64 * 1024):
		if chunk:
			chunks.append(chunk)
	data = b"".join(chunks)
	return data, pick_extension(url, content_type)


def _parse_timestamp_arg(timestamp_str: Optional[str]) -> datetime:
	"""
	Parse the externally supplied timestamp if available.
	Expected format: YYYY-MM-DD HH:MM:SS
	Falls back to current time.
	"""
	if timestamp_str:
		try:
			return datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
		except Exception:
			pass
	return datetime.now()


# ----------------------------------------------------------------------
# JELLYFIN API HELPERS
# ----------------------------------------------------------------------
def get_first_user_id(base_url, api_key):
	url = urljoin(base_url.rstrip("/") + "/", "Users")
	headers = {"X-Emby-Token": api_key}
	resp = _get_session().get(url, headers=headers, timeout=_DEFAULT_TIMEOUT)
	resp.raise_for_status()
	for user in resp.json():
		if not user.get("IsHidden", False):
			return user["Id"]
	raise Exception("No enabled user found")


def get_library_id(base_url, api_key, user_id, library_name):
	url = urljoin(base_url.rstrip("/") + "/", f"Users/{user_id}/Views")
	headers = {"X-Emby-Token": api_key}
	resp = _get_session().get(url, headers=headers, timeout=_DEFAULT_TIMEOUT)
	resp.raise_for_status()
	for item in resp.json()["Items"]:
		if item["Name"].lower() == library_name.lower():
			return item["Id"], item.get("CollectionType", "")
	return None, None


def get_library_items(base_url, api_key, user_id, library_id, library_type):
	items = []
	for it in get_library_items_iter(
		base_url, api_key, user_id, library_id, library_type, recursive=False, page_size=100
	):
		items.append(it)
	return items


def _item_type_passes_filter(item_type: str, library_type: str) -> bool:
	lib_type_lower = (library_type or "").lower()
	type_lower = (item_type or "").lower()

	if lib_type_lower in ("series", "tvshows", "tvshow", "shows"):
		return type_lower == "series"
	elif lib_type_lower in ("movie", "movies"):
		return type_lower == "movie"
	elif lib_type_lower == "music":
		# Old logic: allow all items for Music libraries
		return True
	elif lib_type_lower == "musicvideos":
		# Old logic: only allow Artist / MusicVideoAlbum / Folder
		return type_lower in ("artist", "musicvideoalbum", "folder")
	return True


def get_library_items_iter(
	base_url: str,
	api_key: str,
	user_id: str,
	library_id: str,
	library_type: str,
	recursive: bool = False,
	page_size: int = 100,
) -> Generator[dict, None, None]:
	headers = {"X-Emby-Token": api_key}
	start_index = 0
	lib_type_lower = (library_type or "").lower()

	while True:
		url = urljoin(
			base_url.rstrip("/") + "/",
			f"Users/{user_id}/Items"
			f"?ParentId={library_id}"
			f"&Recursive={'true' if recursive else 'false'}"
			f"&StartIndex={start_index}"
			f"&Limit={page_size}",
		)
		resp = _get_session().get(url, headers=headers, timeout=_DEFAULT_TIMEOUT)
		resp.raise_for_status()
		data = resp.json()
		page_items = data.get("Items", []) or []

		for item in page_items:
			type_lower = (item.get("Type") or "").lower()

			if lib_type_lower in ("series", "tvshows", "tvshow", "shows"):
				if type_lower != "series":
					continue
				yield item
				continue

			if lib_type_lower in ("movie", "movies"):
				if type_lower != "movie":
					continue
				yield item
				continue

			if lib_type_lower == "music":
				# Old logic: yield everything for Music libraries
				yield item
				continue

			if lib_type_lower == "musicvideos":
				# Old logic: only Artist / MusicVideoAlbum / Folder
				if type_lower in ("artist", "musicvideoalbum", "folder"):
					yield item
				continue

			yield item

		if len(page_items) < page_size:
			break
		start_index += page_size


def _is_series_library(library_type: Optional[str]) -> bool:
	lt = (library_type or "").strip().lower()
	return lt in {"tvshows", "tvshow", "shows", "series"}


def _parse_season_number(season: dict) -> Optional[int]:
	idx = season.get("IndexNumber")
	if isinstance(idx, int):
		return idx

	name = str(season.get("Name") or "").strip()

	if name.lower() in {"specials", "season 0", "special features"}:
		return 0

	m = re.search(r"season\s+(\d+)", name, re.IGNORECASE)
	if m:
		try:
			return int(m.group(1))
		except Exception:
			pass

	m = re.search(r"\b(\d+)\b", name)
	if m:
		try:
			return int(m.group(1))
		except Exception:
			pass

	return None


def get_series_seasons(base_url: str, api_key: str, user_id: str, series_id: str) -> List[dict]:
	headers = {"X-Emby-Token": api_key}
	url = urljoin(
		base_url.rstrip("/") + "/",
		f"Users/{user_id}/Items"
		f"?ParentId={series_id}"
		f"&IncludeItemTypes=Season"
		f"&Recursive=false"
		f"&SortBy=SortName"
		f"&SortOrder=Ascending"
		f"&Fields=PrimaryImageAspectRatio"
	)

	resp = _get_session().get(url, headers=headers, timeout=_DEFAULT_TIMEOUT)
	resp.raise_for_status()
	items = resp.json().get("Items", []) or []

	seasons = [it for it in items if (it.get("Type") or "").lower() == "season"]

	def _season_sort_key(season: dict):
		num = _parse_season_number(season)
		name = str(season.get("Name") or "").lower()
		return (999999 if num is None else num, name)

	seasons.sort(key=_season_sort_key)
	return seasons


def get_season_primary_image_url(season: dict, base_url: str, api_key: str) -> Optional[str]:
	season_id = season.get("Id")
	if not season_id:
		return None

	image_tags = season.get("ImageTags", {}) or {}
	tag = image_tags.get("Primary")

	if tag:
		return f"{base_url.rstrip('/')}/Items/{season_id}/Images/Primary?tag={tag}&api_key={api_key}"

	url = f"{base_url.rstrip('/')}/Items/{season_id}/Images/Primary?api_key={api_key}"
	try:
		width, _ = get_image_resolution(url)
		if width:
			return url
	except Exception:
		pass

	return None


# ----------------------------------------------------------------------
# MEMORY-FRIENDLY IMAGE SIZE PROBING
# ----------------------------------------------------------------------
def _probe_image_size_stream(resp_raw) -> Tuple[int, int]:
	parser = ImageFile.Parser()
	chunk_size = 16 * 1024
	while True:
		chunk = resp_raw.read(chunk_size)
		if not chunk:
			break
		parser.feed(chunk)
		try:
			if parser.image:
				return parser.image.size
		except Exception:
			pass
	try:
		img = parser.close()
		if img:
			return img.size
	except Exception:
		pass
	return (0, 0)


def get_image_resolution(url):
	try:
		with _get_session().get(url, stream=True, timeout=_DEFAULT_TIMEOUT) as resp:
			resp.raise_for_status()
			if hasattr(resp, "raw") and resp.raw:
				return _probe_image_size_stream(resp.raw)

			prefix = resp.content[: 64 * 1024]
			parser = ImageFile.Parser()
			parser.feed(prefix)
			if parser.image:
				return parser.image.size
			try:
				img = parser.close()
				if img:
					return img.size
			except Exception:
				pass
			return (0, 0)
	except Exception:
		return (0, 0)


def find_image_tags(item, image_type, base_url, api_key, first_only=False):
	image_tags_dict = item.get("ImageTags", {}) or {}
	tags = []
	image_type_lower = (image_type or "").lower()

	backdrop_tags = []
	if image_type_lower == "backdrop":
		backdrop_tags = item.get("BackdropImageTags", []) or []
		for idx, tag in enumerate(backdrop_tags):
			url = f"{base_url.rstrip('/')}/Items/{item['Id']}/Images/Backdrop/{idx}?tag={tag}&api_key={api_key}"
			width, height = get_image_resolution(url)
			label = "Backdrop" if len(backdrop_tags) == 1 else f"Backdrop ({idx})"
			tags.append((label, url, width, height))
			if first_only:
				return tags

	for key, tag in image_tags_dict.items():
		key_lower = (key or "").lower()
		if key_lower.startswith(image_type_lower):
			url = f"{base_url.rstrip('/')}/Items/{item['Id']}/Images/{image_type}?tag={tag}&api_key={api_key}"
			width, height = get_image_resolution(url)
			tags.append((image_type, url, width, height))
			if first_only:
				return tags

	if not tags:
		url = f"{base_url.rstrip('/')}/Items/{item['Id']}/Images/{image_type}?api_key={api_key}"
		width, height = get_image_resolution(url)
		if width != 0:
			tags.append((image_type, url, width, height))

	return tags


# ----------------------------------------------------------------------
# HTML GENERATION
# ----------------------------------------------------------------------
def _build_caption_html(itype: str, w: int, h: int, low: bool) -> str:
	extra = " - LOW RESOLUTION" if low else ""
	cls = "resolution lowres" if low else "resolution"
	return f'<div class="{cls}">{itype} {w}x{h}{extra}</div>'


def _build_issue_list_html(missing_types: List[str], lowres_types: List[str]) -> str:
	parts = []
	if missing_types:
		missing_html = "<br>".join(missing_types)
		parts.append(
			f'<div class="issue-block"><div class="issue-heading">Missing:</div><div>{missing_html}</div></div>'
		)
	if lowres_types:
		lowres_html = "<br>".join(lowres_types)
		parts.append(
			f'<div class="issue-block"><div class="issue-heading">Low Resolution:</div><div>{lowres_html}</div></div>'
		)
	if not parts:
		return ""
	return f'<div class="missing-list">{"".join(parts)}</div>'


def _write_html_header(fp, bgcolor, textcolor, tablebgcolor, library_name, timestamp):
	fp.write(f"""<html>
<head>
<meta charset="utf-8">
<title>Jellyfin Images - {library_name}</title>
<style>
body {{ font-family: sans-serif; font-size: 18px; background-color: {bgcolor}; color: {textcolor}; }}
h1 {{ font-size: 36px; margin-bottom: 20px; }}
h2 {{ font-size: 28px; margin: 20px 0 20px 0; text-align: center; }}
.movie {{ margin-bottom: 50px; display: flex; flex-direction: column; border:2px solid #555; padding:15px; border-radius:10px; }}
.image-row {{ display: flex; gap: 16px; margin-top:15px; align-items: stretch; }}
.left-column {{ flex: 0 0 33%; display:flex; flex-direction:column; min-width:0; }}
.right-column {{ flex: 0 0 67%; display:flex; flex-direction:column; gap:10px; min-width:0; }}
.image-grid {{ position:relative; margin-bottom:10px; }}
.image-grid img {{ width: 100%; height: auto; display:block; cursor: pointer; border: 2px solid #ccc; border-radius: 5px; }}
.logo-img {{ width:60%; height:auto; display:block; }}
.banner-full {{ width:100%; height:auto; display:block; }}
.box-row {{ display:flex; gap:10px; }}
.box-row .image-grid {{ flex:1 1 0; }}
.box-row .placeholder {{ height:150px; }}
.lightbox {{
	display: none; position: fixed; z-index: 999; padding-top: 60px;
	left: 0; top: 0; width: 100%; height: 100%; overflow: auto; background-color: rgba(0,0,0,0.9);
}}
.lightbox-content {{ position: relative; margin:auto; max-width:90%; max-height:90%; text-align:center; }}
.lightbox-caption {{ color:#fff; font-size:18px; margin-bottom:10px; }}
.lightbox-content img {{ max-width:100%; max-height:80vh; margin-top:10px; cursor:pointer; }}
.lightbox-buttons {{ margin-top:10px; }}
.lightbox-buttons button {{ font-size:16px; padding:10px 16px; min-width:110px; line-height:1; border-radius:6px; }}
table {{ border-collapse: collapse; margin-bottom: 40px; width: 100%; background-color: {tablebgcolor}; }}
th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; font-size: 18px; color: {textcolor}; }}
th {{ background-color: rgba(200,200,200,0.2); }}
.missing-list {{ color:red; font-weight:bold; text-align:center; margin-top:auto; padding-top: 8px; }}
.issue-block {{ margin-top: 12px; }}
.issue-heading {{ margin-bottom: 4px; }}
.placeholder {{ border:2px dashed red; border-radius:5px; color:red; font-weight:bold; display:flex; align-items:center; justify-content:center; height:150px; }}
a {{ color: {textcolor}; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.backlink {{ margin-bottom: 20px; }}
.scroll-top {{ text-align:center; margin-top:10px; }}
.entry-title {{ margin-bottom:15px; }}
.resolution {{ font-size:14px; opacity:0.9; }}
.lowres {{ color: #ff6767; font-weight: bold; }}
.missing {{ color: #ffb347; font-weight: bold; }}
.pixelfin-corner {{
	position: absolute;
	top: 14px;
	left: 14px;
	z-index: 1000;
}}
.pixelfin-corner img {{
	width: 140px;
	height: auto;
	display: block;
}}
</style>
</head>
<body>
""")
	if PIXELFIN_LOGO_BASE64:
		fp.write(
			f'<div class="pixelfin-corner">'
			f'<img src="data:image/png;base64,{PIXELFIN_LOGO_BASE64}" alt="Pixelfin" />'
			f"</div>\n"
		)
	fp.write(f"""
<div style="display:flex;align-items:center;justify-content:flex-end;">
	<div class="backlink" id="top"><a href="/">← Back to Main Page</a></div>
</div>
<h1>{library_name}</h1>
<p>Generated: {timestamp}</p>
""")


def _write_summary_table_open(fp, image_types: List[str]):
	fp.write("<h2>Missing / Low Resolution Images Summary</h2>\n")
	fp.write("<table><tr><th>Item Name</th>")
	for code in image_types:
		fp.write(f"<th>{IMAGE_TYPES_MAP.get(code, code)}</th>")
	fp.write("</tr>\n")


def _write_summary_table_row(fp, item_id: str, safe_name: str, image_types: List[str],
							 missing_types: List[str], lowres_types: List[str]):
	fp.write(f'<tr><td><a href="#item_{item_id}">{safe_name}</a></td>')
	for code in image_types:
		tname = IMAGE_TYPES_MAP.get(code)
		if tname in missing_types:
			fp.write('<td class="missing">Missing</td>')
		elif tname in lowres_types:
			fp.write('<td class="lowres">Low</td>')
		else:
			fp.write("<td></td>")
	fp.write("</tr>\n")


def _write_summary_table_close(fp):
	fp.write("</table>\n")


def _write_lightbox(fp):
	fp.write("""
	<div id="lightbox" class="lightbox" onclick="clickOutside(event)">
	  <div class="lightbox-content">
		<div class="lightbox-caption" id="lightbox-caption"></div>
		<img id="lightbox-img" src="" alt="" />
		<div class="lightbox-buttons">
		  <button onclick="prevImage(event)">◀ Prev</button>
		  <button onclick="nextImage(event)">Next ▶</button>
		  <button onclick="closeLightbox()">Close ✖</button>
		</div>
	  </div>
	</div>
	<script>
	let currentImages = [];
	let currentIndex = 0;

	function openLightbox(entryId, src) {
	  currentImages = [];
	  const imgs = document.querySelectorAll("#item_"+entryId+" img");
	  imgs.forEach(i => currentImages.push({src: i.src, caption: i.alt || ""}));
	  const idx = currentImages.findIndex(i => i.src === src);
	  currentIndex = idx >= 0 ? idx : 0;
	  showImage();
	  document.getElementById('lightbox').style.display='block';
	}

	function showImage() {
	  if(!currentImages.length) return;
	  const img = document.getElementById('lightbox-img');
	  const { src, caption } = currentImages[currentIndex];
	  img.src = src;
	  img.alt = caption;
	  document.getElementById('lightbox-caption').innerText = caption;
	}

	document.addEventListener('DOMContentLoaded', () => {
	  const lightboxImg = document.getElementById('lightbox-img');
	  if (lightboxImg) {
		lightboxImg.addEventListener('click', (e) => {
		  e.preventDefault();
		  e.stopPropagation();
		  nextImage(e);
		});
	  }
	});

	function closeLightbox() {
	  document.getElementById('lightbox').style.display='none';
	  currentImages = [];
	  currentIndex = 0;
	}

	function prevImage(e){ e.stopPropagation(); if(!currentImages.length) return; currentIndex=(currentIndex-1+currentImages.length)%currentImages.length; showImage(); }
	function nextImage(e){ e.stopPropagation(); if(!currentImages.length) return; currentIndex=(currentIndex+1)%currentImages.length; showImage(); }
	function clickOutside(e){ if(e.target.id==='lightbox'){ closeLightbox(); } }

	document.addEventListener('keydown', function(e){
	  if(e.key==='Escape') closeLightbox();
	  else if(e.key==='ArrowLeft') prevImage(e);
	  else if(e.key==='ArrowRight') nextImage(e);
	});
	</script>
	""")


def _write_footer(fp):
	fp.write("</body></html>\n")


def generate_html(
	items,
	image_types,
	base_url,
	api_key,
	output_file,
	bgcolor,
	textcolor,
	tablebgcolor,
	library_type,
	library_name,
	timestamp,
	minres,
):
	if not os.path.isabs(output_file):
		output_file = os.path.join(BASE_DIR, output_file)

	os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)

	tmp_dir = tempfile.mkdtemp(prefix="jf_html_")
	body_tmp_path = os.path.join(tmp_dir, "body_sections.html")

	left_codes = [c for c in LEFT_TYPES if c in image_types]
	right_codes = [c for c in RIGHT_TYPES if c in image_types]

	name_map = build_item_display_name_map(items, library_type)
	summary_rows = []

	try:
		for item in items:
			item_id = item.get("Id")
			if not item_id:
				continue

			safe_name = name_map.get(item_id, sanitize_folder_name(_safe_name(item)))
			missing_types: List[str] = []
			lowres_types: List[str] = []
			summary_rows.append((item_id, safe_name, missing_types, lowres_types))

		summary_by_id = {
			item_id: (missing_types, lowres_types)
			for item_id, _, missing_types, lowres_types in summary_rows
		}

		with open(body_tmp_path, "w", encoding="utf-8") as body_fp:
			for item in items:
				item_id = item.get("Id")
				if not item_id:
					continue

				missing_types, lowres_types = summary_by_id.get(item_id, ([], []))

				def _mark(lst, val):
					if val and val not in lst:
						lst.append(val)

				safe_name = name_map.get(item_id, sanitize_folder_name(_safe_name(item)))

				left_html_parts = []
				right_html_parts = []

				for code in left_codes:
					image_type_name = IMAGE_TYPES_MAP.get(code, code)
					tags = find_image_tags(item, image_type_name, base_url, api_key)
					if tags:
						for itype, url, w, h in tags:
							low = check_low_res(code, w, h, minres)
							if low:
								_mark(lowres_types, image_type_name)
							alt_caption = f"{safe_name} - {itype} ({w}x{h})" + (" - LOW RESOLUTION" if low else "")
							left_html_parts.append(f"""
<div class="image-grid">
  <img src="{url}" alt="{alt_caption}" loading="lazy"
   onclick="openLightbox('{item_id}', '{url}'); return false;"
   style="cursor:pointer; border:2px solid #ccc; border-radius:5px;">
  {_build_caption_html(itype, w, h, low)}
</div>""")
					else:
						_mark(missing_types, image_type_name)
						left_html_parts.append(
							f'<div class="image-grid"><div class="placeholder">Missing: {image_type_name}</div></div>\n'
						)

				# render normal right-column items first, excluding box/boxrear/disc
				box_codes = ["b", "br", "d"]
				normal_right_codes = [c for c in right_codes if c not in box_codes and c != "l"]
				
				for code in normal_right_codes:
					image_type_name = IMAGE_TYPES_MAP.get(code, code)
					tags = find_image_tags(item, image_type_name, base_url, api_key)
					if tags:
						for itype, url, w, h in tags:
							low = check_low_res(code, w, h, minres)
							if low:
								_mark(lowres_types, image_type_name)
							alt_caption = f"{safe_name} - {itype} ({w}x{h})" + (" - LOW RESOLUTION" if low else "")
							right_html_parts.append(f"""
				<div class="image-grid">
				  <img src="{url}" alt="{alt_caption}" loading="lazy"
				   onclick="openLightbox('{item_id}', '{url}'); return false;"
				   style="cursor:pointer; border:2px solid #ccc; border-radius:5px;">
				  {_build_caption_html(itype, w, h, low)}
				</div>""")
					else:
						_mark(missing_types, image_type_name)
						right_html_parts.append(
							f'<div class="image-grid"><div class="placeholder">Missing: {image_type_name}</div></div>\n'
						)
				
				# render Box, BoxRear, and Disc in one horizontal row
				box_row_parts = []
				
				for code in box_codes:
					if code not in right_codes:
						continue
				
					image_type_name = IMAGE_TYPES_MAP.get(code, code)
					tags = find_image_tags(item, image_type_name, base_url, api_key)
				
					if tags:
						for itype, url, w, h in tags:
							low = check_low_res(code, w, h, minres)
							if low:
								_mark(lowres_types, image_type_name)
							alt_caption = f"{safe_name} - {itype} ({w}x{h})" + (" - LOW RESOLUTION" if low else "")
							box_row_parts.append(f"""
				<div class="image-grid">
				  <img src="{url}" alt="{alt_caption}" loading="lazy"
				   onclick="openLightbox('{item_id}', '{url}'); return false;"
				   style="cursor:pointer; border:2px solid #ccc; border-radius:5px;">
				  {_build_caption_html(itype, w, h, low)}
				</div>""")
					else:
						_mark(missing_types, image_type_name)
						box_row_parts.append(
							f'<div class="image-grid"><div class="placeholder">Missing: {image_type_name}</div></div>'
						)
				
				if box_row_parts:
					right_html_parts.append(f"""
				<div class="box-row">
					{''.join(box_row_parts)}
				</div>""")

				if "l" in right_codes:
					tags = find_image_tags(item, "Logo", base_url, api_key)
					if tags:
						for itype, url, w, h in tags:
							low = check_low_res("l", w, h, minres)
							if low:
								_mark(lowres_types, "Logo")
							alt_caption = f"{safe_name} - {itype} ({w}x{h})" + (" - LOW RESOLUTION" if low else "")
							right_html_parts.append(f"""
<div class="image-grid">
  <img src="{url}" class="logo-img" alt="{alt_caption}" loading="lazy"
   onclick="openLightbox('{item_id}', '{url}'); return false;"
   style="cursor:pointer; border:2px solid #ccc; border-radius:5px;">
  {_build_caption_html(itype, w, h, low)}
</div>""")
					else:
						_mark(missing_types, "Logo")
						right_html_parts.append('<div class="placeholder">Missing: Logo</div>\n')

				issue_html = _build_issue_list_html(missing_types, lowres_types)

				body_fp.write(f'<div class="movie" id="item_{item_id}">\n')

				link_url = f"{base_url.rstrip('/')}/web/index.html#!/details?id={item_id}"
				display_title = safe_name

				body_fp.write(
					f'<h2 class="entry-title">'
					f'<a target="_blank" rel="noopener noreferrer" href="{link_url}">{display_title}</a>'
					f"</h2>\n"
				)

				body_fp.write('<div class="image-row">\n')

				body_fp.write('<div class="left-column">\n')
				for chunk in left_html_parts:
					body_fp.write(chunk)
				if issue_html:
					body_fp.write(issue_html + "\n")
				body_fp.write("</div>\n")

				body_fp.write('<div class="right-column">\n')
				for chunk in right_html_parts:
					body_fp.write(chunk)
				body_fp.write("</div>\n")

				body_fp.write("</div>\n")
				body_fp.write('<div class="scroll-top"><a href="#top">↑ Scroll to Top</a></div>\n')
				body_fp.write("</div>\n")

		with open(output_file, "w", encoding="utf-8") as out_fp:
			_write_html_header(out_fp, bgcolor, textcolor, tablebgcolor, library_name, timestamp)
			_write_summary_table_open(out_fp, image_types)
			for item_id, safe_name, missing_types, lowres_types in summary_rows:
				_write_summary_table_row(out_fp, item_id, safe_name, image_types, missing_types, lowres_types)
			_write_summary_table_close(out_fp)
			with open(body_tmp_path, "r", encoding="utf-8") as body_fp:
				for line in body_fp:
					out_fp.write(line)
			_write_lightbox(out_fp)
			_write_footer(out_fp)

		print(f"HTML file generated: {output_file}")
	finally:
		shutil.rmtree(tmp_dir, ignore_errors=True)


# ----------------------------------------------------------------------
# ZIP GENERATION
# ----------------------------------------------------------------------
def create_zip(
	items,
	image_types: List[str],
	base_url: str,
	api_key: str,
	zip_output_file: str,
	library_name: str,
	library_type: str,
	zip_basename_overrides: Optional[Dict[str, str]] = None,
	user_id: Optional[str] = None,
):
	if not os.path.isabs(zip_output_file):
		zip_output_file = os.path.join(BASE_DIR, zip_output_file)

	os.makedirs(os.path.dirname(zip_output_file) or ".", exist_ok=True)

	name_overrides = dict(DEFAULT_ZIP_BASENAMES)
	if zip_basename_overrides:
		for code, name in zip_basename_overrides.items():
			if code in name_overrides and isinstance(name, str) and name.strip():
				name_overrides[code] = name.strip()

	name_map = build_item_display_name_map(items, library_type)
	is_series_library = _is_series_library(library_type)

	with ZipFile(zip_output_file, "w", compression=ZIP_DEFLATED) as zf:
		for item in items:
			item_id = item.get("Id")
			if not item_id:
				continue

			folder = sanitize_folder_name(name_map.get(item_id, _safe_name(item)))

			for code in image_types:
				image_type_name = IMAGE_TYPES_MAP.get(code)
				if not image_type_name:
					continue

				tags = find_image_tags(item, image_type_name, base_url, api_key, first_only=False)
				if not tags:
					continue

				base_name = name_overrides.get(code, DEFAULT_ZIP_BASENAMES.get(code, image_type_name.lower()))
				multi = len(tags) > 1

				for idx, (_, url, _, _) in enumerate(tags, start=1):
					try:
						data, ext = stream_to_bytes(url)
						filename = f"{base_name}{idx:02d}{ext}" if multi else f"{base_name}{ext}"
						arcname = f"{folder}/{filename}"
						zf.writestr(arcname, data)
						print(f"Added: {arcname}")
					except Exception as e:
						print(f"Failed to add image for item '{_safe_name(item)}' ({image_type_name}): {e}")

			if is_series_library and user_id and (item.get("Type") or "").lower() == "series":
				try:
					seasons = get_series_seasons(base_url, api_key, user_id, item_id)
				except Exception as e:
					print(f"Failed to fetch seasons for series '{_safe_name(item)}': {e}")
					seasons = []

				for season in seasons:
					try:
						season_num = _parse_season_number(season)
						season_url = get_season_primary_image_url(season, base_url, api_key)
						if not season_url:
							continue

						data, ext = stream_to_bytes(season_url)

						if season_num is not None:
							filename = f"season{season_num:02d}-poster{ext}"
						else:
							season_name = sanitize_folder_name(season.get("Name", "season"))
							filename = f"{season_name}{ext}"

						arcname = f"{folder}/{filename}"
						zf.writestr(arcname, data)
						print(f"Added: {arcname}")
					except Exception as e:
						print(f"Failed to add season poster for series '{_safe_name(item)}': {e}")

	print(f"ZIP file created: {zip_output_file}")


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Generate HTML gallery from Jellyfin library")
	parser.add_argument("--server", required=True)
	parser.add_argument("--apikey", required=True)
	parser.add_argument("--library", required=True)
	parser.add_argument("--output", default="gallery.html")
	parser.add_argument("--bgcolor", default="#222")
	parser.add_argument("--textcolor", default="#eee")
	parser.add_argument("--tablebgcolor", default="#333")
	parser.add_argument("--images", default="p,t,c,m,bd,bn,b,br,d,l")
	parser.add_argument("--minres", default="", help='Semicolon-separated list like "bd:3840x2160;p:2000x3000"')
	parser.add_argument("--timestamp", default=None, help="Optional timestamp string to embed in HTML")
	parser.add_argument("--zip-output", default=None, help="If provided, create ZIP at this path")
	parser.add_argument("--zipnames", default=None, help="JSON of code->basename (no extension) overrides for ZIP creation")
	parser.add_argument(
		"--sort",
		choices=["alphabetical", "recent"],
		default="alphabetical",
		help="Sort order for library items (alphabetical or recent)",
	)

	args = parser.parse_args()

	image_types = [c for c in args.images.split(",") if c in IMAGE_TYPES_MAP]
	minres = parse_minres_arg(args.minres)

	try:
		user_id = get_first_user_id(args.server, args.apikey)
		library_id, library_type = get_library_id(args.server, args.apikey, user_id, args.library)
		if not library_id:
			print(f"Library '{args.library}' not found for user.")
			sys.exit(1)

		items = get_library_items(args.server, args.apikey, user_id, library_id, library_type)

		full_items = []
		session = _get_session()

		for it in items:
			item_id = it.get("Id")
			if not item_id:
				continue

			data = None

			try:
				url_sys = f"{args.server.rstrip('/')}/Items/{item_id}?api_key={args.apikey}"
				r = session.get(url_sys, timeout=10)
				r.raise_for_status()
				data = r.json()
			except Exception:
				pass

			if not data:
				try:
					url_usr = f"{args.server.rstrip('/')}/Users/{user_id}/Items/{item_id}?api_key={args.apikey}"
					r = session.get(url_usr, timeout=10)
					r.raise_for_status()
					data = r.json()
				except Exception:
					data = dict(it)

			data["Id"] = item_id
			full_items.append(data)

		if args.sort == "recent":
			print("Building Date Added cache for top-level items...")
			session.headers.update({"X-Emby-Token": args.apikey})
			date_cache = {}

			def parse_item_datetime(date_str):
				if not date_str:
					return datetime.min.replace(tzinfo=timezone.utc)
				try:
					dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
					if dt.tzinfo is None:
						return dt.replace(tzinfo=timezone.utc)
					return dt
				except Exception:
					return datetime.min.replace(tzinfo=timezone.utc)

			def fetch_item_meta(item_id):
				meta_url = f"{args.server.rstrip('/')}/Users/{user_id}/Items/{item_id}"
				resp = session.get(meta_url, timeout=15)
				resp.raise_for_status()
				return resp.json()

			def fetch_effective_date(item_obj):
				item_id = item_obj.get("Id")
				if not item_id:
					return datetime.min.replace(tzinfo=timezone.utc)
				if item_id in date_cache:
					return date_cache[item_id]["date"]

				item_type = (item_obj.get("Type") or "").lower()
				best_date = parse_item_datetime(item_obj.get("DateAdded") or item_obj.get("DateCreated"))

				try:
					meta = fetch_item_meta(item_id)
					best_date = max(best_date, parse_item_datetime(meta.get("DateAdded") or meta.get("DateCreated")))
				except Exception as e:
					print(f"Failed to fetch metadata for {item_obj.get('Name', '(unknown)')}: {e}")

				if item_type in ("series", "boxset", "folder", "collectionfolder", "userview"):
					children_url = (
						f"{args.server.rstrip('/')}/Users/{user_id}/Items"
						f"?ParentId={item_id}&Recursive=true"
					)
					try:
						resp = session.get(children_url, timeout=15)
						resp.raise_for_status()
						for child in resp.json().get("Items", []):
							child_date = parse_item_datetime(child.get("DateAdded") or child.get("DateCreated"))
							if child_date > best_date:
								best_date = child_date
					except Exception as e:
						print(f"Failed to list descendants for {item_obj.get('Name', '(unknown)')}: {e}")

				date_cache[item_id] = {"date": best_date, "name": item_obj.get("Name", "(unknown)")}
				return best_date

			for it in full_items:
				it["_parsed_date"] = fetch_effective_date(it)

			full_items.sort(key=lambda x: x["_parsed_date"], reverse=True)
			print(f"Collected {len(date_cache)} top-level timestamps.")
			print("Sorted by Date Added (newest first).")
		else:
			full_items.sort(key=lambda x: str(x.get("Name", "")).lower())

		items = full_items

		safe_lib = safe_library_name(args.library)
		output_dir = os.path.join("output", safe_lib)
		os.makedirs(output_dir, exist_ok=True)

		if args.zip_output:
			try:
				overrides = json.loads(args.zipnames) if args.zipnames else {}
			except Exception:
				overrides = {}

			create_zip(
				items,
				image_types,
				args.server,
				args.apikey,
				args.zip_output,
				args.library,
				library_type,
				overrides,
				user_id=user_id,
			)
			sys.exit(0)

		run_dt = _parse_timestamp_arg(args.timestamp)
		timestamp = run_dt.strftime("%Y-%m-%d %H:%M:%S")
		file_timestamp = run_dt.strftime("%Y-%m-%d_%H-%M-%S")

		# IMPORTANT:
		# Do not prune HTML files here. Retention is handled centrally in app.py
		# by _prune_outputs_for_library(), which correctly keeps the newest N
		# non-kept files *in addition to* any files marked Keep.
		#
		# The previous inline cleanup here kept only the newest HTML per variant
		# (Alphabetical / Date-Added / other) before app.py's retention logic ran.
		# That made HTML behave differently from ZIP files and caused auto mode to
		# ignore the configured keep_html count whenever multiple generated HTMLs
		# existed.

		if args.output and args.output != "gallery.html":
			output_path = args.output
		else:
			sort_label = "Alphabetical" if args.sort == "alphabetical" else "Date-Added"
			html_name = f"{file_timestamp} - {safe_lib} - {sort_label}.html"
			output_path = os.path.join(output_dir, html_name)

		sort_label = "Alphabetical" if args.sort == "alphabetical" else "Date-Added"
		top_level_count = len([it for it in full_items if _item_type_passes_filter(it.get("Type", ""), library_type)])
		print(f"Collected {top_level_count} top-level items for {sort_label} sort.")
		print(f"Writing {sort_label} HTML -> {output_path}")

		generate_html(
			items,
			image_types,
			args.server,
			args.apikey,
			output_path,
			args.bgcolor,
			args.textcolor,
			args.tablebgcolor,
			library_type,
			args.library,
			timestamp,
			minres,
		)

	except requests.HTTPError as e:
		print(f"HTTP error: {e}")
		sys.exit(1)
	except requests.RequestException as e:
		print(f"Request failed: {e}")
		sys.exit(1)
	except Exception as e:
		print(f"Error: {e}")
		sys.exit(1)
