import requests
import argparse
import sys
import os
import warnings
import tempfile
import json
from urllib.parse import urljoin
from io import BytesIO
from PIL import Image, ImageFile
from datetime import datetime
from typing import Dict, Tuple, Iterable, Generator, List, Optional
from zipfile import ZipFile, ZIP_DEFLATED
import re
import mimetypes

# Keep original warning suppression behavior
warnings.simplefilter('ignore', Image.DecompressionBombWarning)

# Reuse parser to avoid full image load
ImageFile.LOAD_TRUNCATED_IMAGES = True

# Base directory of this script (used to resolve relative output paths safely)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------
# ORIGINAL CONSTANTS (PRESERVED)
# ----------------------------------------------------------------------

# Map short codes to Jellyfin image types
IMAGE_TYPES_MAP = {
	'p': 'Primary',
	'c': 'ClearArt',
	'bd': 'Backdrop',
	'bn': 'Banner',
	'b': 'Box',
	'br': 'BoxRear',
	'd': 'Disc',
	'l': 'Logo',
	'm': 'Menu',
	't': 'Thumb'
}
# Reverse map for lookup by ImageTypeName
IMAGE_TYPES_REVERSE = {v: k for k, v in IMAGE_TYPES_MAP.items()}

# Columns and display order
# Left: Primary → Thumb → ClearArt → Menu (all full width)
LEFT_TYPES = ['p', 't', 'c', 'm']
# Right: Backdrop (full) → Banner (full) → Box → BoxRear → Disc (row, 1/3 each) → Logo (60% width, left)
RIGHT_TYPES = ['bd', 'bn', 'b', 'br', 'd', 'l']

# Defaults used for ZIP filename overrides (base names, no extension)
DEFAULT_ZIP_BASENAMES = {
	'p': 'cover',
	't': 'thumbnail',
	'bd': 'backdrop',
	'c': 'clearart',
	'bn': 'banner',
	'b': 'box',
	'br': 'boxrear',
	'd': 'disc',
	'l': 'logo',
	'm': 'menu'
}

# ----------------------------------------------------------------------
# NEW: Shared requests session with timeouts for efficiency & stability
# ----------------------------------------------------------------------

_DEFAULT_TIMEOUT = (10, 30)  # (connect, read) seconds; conservative but safe
_session: Optional[requests.Session] = None

def _get_session() -> requests.Session:
	global _session
	if _session is None:
		_session = requests.Session()
		_session.headers.update({'User-Agent': 'generate_html.py (memory-friendly)'})
	return _session

# ----------------------------------------------------------------------
# ORIGINAL FUNCTIONS (with memory-friendly internal improvements)
# ----------------------------------------------------------------------

def parse_minres_arg(minres_str):
	result = {}
	if not minres_str:
		return result
	parts = [p.strip() for p in minres_str.split(';') if p.strip()]
	for part in parts:
		try:
			code, wh = part.split(':', 1)
			w, h = wh.lower().split('x', 1)
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

def get_first_user_id(base_url, api_key):
	url = urljoin(base_url.rstrip('/') + '/', 'Users')
	headers = {'X-Emby-Token': api_key}
	resp = _get_session().get(url, headers=headers, timeout=_DEFAULT_TIMEOUT)
	resp.raise_for_status()
	for user in resp.json():
		if not user.get('IsHidden', False):
			return user['Id']
	raise Exception("No enabled user found")

def get_library_id(base_url, api_key, user_id, library_name):
	url = urljoin(base_url.rstrip('/') + '/', f'Users/{user_id}/Views')
	headers = {'X-Emby-Token': api_key}
	resp = _get_session().get(url, headers=headers, timeout=_DEFAULT_TIMEOUT)
	resp.raise_for_status()
	for item in resp.json()['Items']:
		if item['Name'].lower() == library_name.lower():
			return item['Id'], item.get('CollectionType', '')
	return None, None

def get_library_items(base_url, api_key, user_id, library_id, library_type):
	items = []
	for it in get_library_items_iter(base_url, api_key, user_id, library_id, library_type,
									 recursive=False, page_size=100):
		items.append(it)
	return items

# ----------------------------------------------------------------------
# NEW: Generator-based item retrieval for low memory usage
# ----------------------------------------------------------------------

def _item_type_passes_filter(item_type: str, library_type: str) -> bool:
	lib_type_lower = (library_type or '').lower()
	type_lower = (item_type or '').lower()
	if lib_type_lower == 'series' and type_lower != 'series':
		return False
	elif lib_type_lower == 'movie' and type_lower != 'movie':
		return False
	elif lib_type_lower == 'music':
		return True
	elif lib_type_lower == 'musicvideos':
		return type_lower in ('artist', 'musicvideoalbum', 'folder')
	return True

def get_library_items_iter(base_url: str,
						   api_key: str,
						   user_id: str,
						   library_id: str,
						   library_type: str,
						   recursive: bool = False,
						   page_size: int = 100) -> Generator[dict, None, None]:
	headers = {'X-Emby-Token': api_key}
	start_index = 0
	lib_type_lower = (library_type or '').lower()
	while True:
		url = urljoin(
			base_url.rstrip('/') + '/',
			f'Users/{user_id}/Items?ParentId={library_id}&Recursive={"true" if recursive else "false"}&StartIndex={start_index}&Limit={page_size}'
		)
		resp = _get_session().get(url, headers=headers, timeout=_DEFAULT_TIMEOUT)
		resp.raise_for_status()
		data = resp.json()
		page_items = data.get('Items', []) or []

		for item in page_items:
			type_lower = (item.get('Type') or '').lower()
			if lib_type_lower == 'series' and type_lower != 'series':
				continue
			elif lib_type_lower == 'movie' and type_lower != 'movie':
				continue
			elif lib_type_lower == 'music':
				yield item
				continue
			elif lib_type_lower == 'musicvideos':
				if type_lower in ('artist', 'musicvideoalbum', 'folder'):
					yield item
				continue
			yield item

		if len(page_items) < page_size:
			break
		start_index += page_size

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
			if hasattr(resp, 'raw') and resp.raw:
				return _probe_image_size_stream(resp.raw)
			prefix = resp.content[:64 * 1024]
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
	image_tags_dict = item.get('ImageTags', {}) or {}
	tags = []
	image_type_lower = (image_type or '').lower()
	
	# --- NEW: also check BackdropImageTags / multiple indices ---
	backdrop_tags = []
	if image_type_lower == "backdrop":
		backdrop_tags = item.get('BackdropImageTags', []) or []
		for idx, tag in enumerate(backdrop_tags):
			url = f"{base_url.rstrip('/')}/Items/{item['Id']}/Images/Backdrop/{idx}?tag={tag}&api_key={api_key}"
			width, height = get_image_resolution(url)
			# If there's only one backdrop, label just "Backdrop"; otherwise include index in parentheses
			label = "Backdrop" if len(backdrop_tags) == 1 else f"Backdrop ({idx})"
			tags.append((label, url, width, height))
			if first_only:
				return tags
	
	# --- Original generic handling ---
	for key, tag in image_tags_dict.items():
		key_lower = (key or '').lower()
		if key_lower.startswith(image_type_lower):
			url = f"{base_url.rstrip('/')}/Items/{item['Id']}/Images/{image_type}?tag={tag}&api_key={api_key}"
			width, height = get_image_resolution(url)
			tags.append((image_type, url, width, height))
			if first_only:
				return tags
	
	# --- Fallback for untagged images ---
	if not tags:
		url = f"{base_url.rstrip('/')}/Items/{item['Id']}/Images/{image_type}?api_key={api_key}"
		width, height = get_image_resolution(url)
		if width != 0:
			tags.append((image_type, url, width, height))
	
	return tags


# ----------------------------------------------------------------------
# Utility helpers for ZIP creation
# ----------------------------------------------------------------------

_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n]+')

def sanitize_folder_name(name: str) -> str:
	s = _SAFE_NAME_RE.sub('_', name or '')
	s = s.strip().strip('.')
	return s or 'Untitled'

def pick_extension(url: str, content_type: str | None) -> str:
	# Prefer content-type if provided
	if content_type:
		if 'jpeg' in content_type:
			return '.jpg'
		if 'png' in content_type:
			return '.png'
		if 'webp' in content_type:
			return '.webp'
		if 'gif' in content_type:
			return '.gif'
		if 'bmp' in content_type:
			return '.bmp'
	# Fallback to URL suffix
	ext = os.path.splitext(url.split('?',1)[0])[1].lower()
	if ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']:
		return ext if ext != '.jpeg' else '.jpg'
	# Default
	return '.jpg'

def stream_to_bytes(url: str) -> tuple[bytes, str]:
	resp = _get_session().get(url, stream=True, timeout=_DEFAULT_TIMEOUT)
	resp.raise_for_status()
	content_type = resp.headers.get('Content-Type', '')
	chunks = []
	for chunk in resp.iter_content(chunk_size=64 * 1024):
		if chunk:
			chunks.append(chunk)
	data = b''.join(chunks)
	return data, pick_extension(url, content_type)

# ----------------------------------------------------------------------
# LOW-MEMORY HTML GENERATION (PRESERVED with safe-path fix)
# ----------------------------------------------------------------------

def _safe_name(item: dict) -> str:
	return str(item.get("Name", ""))

def _build_caption_html(itype: str, w: int, h: int, low: bool) -> str:
	extra = " - LOW RESOLUTION" if low else ""
	cls = "resolution lowres" if low else "resolution"
	return f'<div class="{cls}">{itype} {w}x{h}{extra}</div>'

def _write_html_header(fp, bgcolor, textcolor, tablebgcolor, library_name, timestamp):
	fp.write(f'''<html>
<head>
<meta charset="utf-8">
<title>Jellyfin Images - {library_name}</title>
<style>
body {{ font-family: sans-serif; font-size: 18px; background-color: {bgcolor}; color: {textcolor}; }}
h1 {{ font-size: 36px; margin-bottom: 20px; }}
h2 {{ font-size: 28px; margin: 20px 0 20px 0; text-align: center; }}
.movie {{ margin-bottom: 50px; display: flex; flex-direction: column; border:2px solid #555; padding:15px; border-radius:10px; }}
.image-row {{ display: flex; gap: 16px; margin-top:15px; }}
.left-column {{ flex: 0 0 33%; display:flex; flex-direction:column; min-width:0; }}
.right-column {{ flex: 0 0 67%; display:flex; flex-direction:column; gap:10px; min-width:0; }}
.image-grid {{ position:relative; margin-bottom:10px; }}
.image-grid img {{ width: 100%; height: auto; display:block; cursor: pointer; border: 2px solid #ccc; border-radius: 5px; }}
/* Logo 60% width, left-aligned */
.logo-img {{ width:60%; height:auto; display:block; }}
/* Banner/Backdrop full width of right column */
.banner-full {{ width:100%; height:auto; display:block; }}
/* Row for Box, BoxRear, Disc (each 1/3 of right column) */
.box-row {{ display:flex; gap:10px; }}
.box-row .image-grid {{ flex:1 1 0; }}
/* Lightbox */
.lightbox {{
	display: none; position: fixed; z-index: 999; padding-top: 60px;
	left: 0; top: 0; width: 100%; height: 100%; overflow: auto; background-color: rgba(0,0,0,0.9);
}}
.lightbox-content {{ position: relative; margin:auto; max-width:90%; max-height:90%; text-align:center; }}
.lightbox-caption {{ color:#fff; font-size:18px; margin-bottom:10px; }}
.lightbox-content img {{ max-width:100%; max-height:80vh; margin-top:10px; cursor:pointer; }}
.lightbox-buttons {{ margin-top:10px; }}
.lightbox-buttons button {{ font-size:16px; padding:10px 16px; min-width:110px; line-height:1; border-radius:6px; }}
/* Table */
table {{ border-collapse: collapse; margin-bottom: 40px; width: 100%; background-color: {tablebgcolor}; }}
th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; font-size: 18px; color: {textcolor}; }}
th {{ background-color: rgba(200,200,200,0.2); }}
/* Missing/low list pinned to bottom of left column */
.missing-list {{ color:red; font-weight:bold; text-align:center; margin-top:auto; }}
/* Visual placeholder boxes for missing images */
.placeholder {{ border:2px dashed red; border-radius:5px; color:red; font-weight:bold; display:flex; align-items:center; justify-content:center; height:150px; }}
a {{ color: {textcolor}; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.backlink {{ margin-bottom: 20px; }}
.scroll-top {{ text-align:center; margin-top:10px; }}
.entry-title {{ margin-bottom:15px; }}
.resolution {{ font-size:14px; opacity:0.9; }}
.lowres {{ color: #ff6767; font-weight: bold; }}
</style>
</head>
<body>
<div class="backlink" id="top"><a href="/">← Back to Main Page</a></div>
<h1>{library_name}</h1>
<p>Generated: {timestamp}</p>
''')

def _write_summary_table_open(fp, image_types: List[str]):
	fp.write('<h2>Missing / Low Resolution Images Summary</h2>\n')
	fp.write('<table><tr><th>Item Name</th>')
	for code in image_types:
		fp.write(f'<th>{IMAGE_TYPES_MAP.get(code, code)}</th>')
	fp.write('</tr>\n')

def _write_summary_table_row(fp, item_id: str, safe_name: str, image_types: List[str],
							 missing_types: List[str], lowres_types: List[str]):
	fp.write(f'<tr><td><a href="#item_{item_id}">{safe_name}</a></td>')
	for code in image_types:
		tname = IMAGE_TYPES_MAP.get(code)
		mark_yes = (tname in missing_types) or (tname in lowres_types)
		fp.write(f'<td>{"Yes" if mark_yes else ""}</td>')
	fp.write('</tr>\n')

def _write_summary_table_close(fp):
	fp.write('</table>\n')

def _write_lightbox(fp):
	fp.write('''
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
	
	// Clicking the displayed image cycles to the next one
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
	''')



def _write_footer(fp):
	fp.write('</body></html>\n')

# ----------------------------------------------------------------------
# CORE: generate_html (unchanged logic; added safe-path + optional timestamp)
# ----------------------------------------------------------------------

def generate_html(items, image_types, base_url, api_key, output_file, bgcolor, textcolor, tablebgcolor,
				  library_type, library_name, timestamp, minres):
	"""
	Memory-friendly implementation that preserves the original output
	while avoiding large in-memory structures.
	Folder disambiguation:
	- Movies: always append year if available.
	- Shows: append year only if duplicate.
	"""
	# Resolve output_file relative to script dir if not absolute
	if not os.path.isabs(output_file):
		output_file = os.path.join(BASE_DIR, output_file)

	# Prepare directories and temp files
	os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)

	# temp files for summary rows, issues, and body
	tmp_dir = tempfile.mkdtemp(prefix="jf_html_")
	summary_tmp_path = os.path.join(tmp_dir, "summary_rows.html")
	body_tmp_path = os.path.join(tmp_dir, "body_sections.html")
	index_tmp_path = os.path.join(tmp_dir, "items_index.jsonl")
	issues_tmp_path = os.path.join(tmp_dir, "issues.jsonl")

	left_codes = [c for c in LEFT_TYPES if c in image_types]
	right_codes = [c for c in RIGHT_TYPES if c in image_types]

	def iter_items_first_pass():
		for it in items:
			yield it

	summary_rows = []
	seen_names: Dict[str, List[str]] = {}

	with open(index_tmp_path, 'w', encoding='utf-8') as index_fp, \
		 open(issues_tmp_path, 'w', encoding='utf-8') as issues_fp:
	
		for item in iter_items_first_pass():
			item_id = item.get('Id')
			title = _safe_name(item)
			folder = title

			# Try to get the production/release year
			year = None
			if "ProductionYear" in item and item["ProductionYear"]:
				year = str(item["ProductionYear"])
			elif "PremiereDate" in item and item["PremiereDate"]:
				try:
					year = str(datetime.fromisoformat(item["PremiereDate"]).year)
				except Exception:
					pass

			# Movies: always append year
			if library_type.lower() == "movies":
				if year:
					folder = f"{folder} ({year})"
				if folder not in seen_names:
					seen_names[folder] = []
				else:
					count = len(seen_names[folder]) + 1
					folder = f"{folder} {count}"
					seen_names[folder].append(str(count))

			else:  # Shows
				if folder in seen_names:
					if year and year not in seen_names[folder]:
						folder = f"{folder} ({year})"
						seen_names[title].append(year)
					else:
						count = len(seen_names[folder]) + 1
						folder = f"{folder} {count}"
						seen_names[folder].append(str(count))
				else:
					if year:
						seen_names[folder] = [year]
					else:
						seen_names[folder] = ["1"]

			safe_name = folder

			index_fp.write(json.dumps({"Id": item_id, "Name": safe_name}) + "\n")
	
			missing_types = []
			lowres_types = []
	
			for code in image_types:
				image_type = IMAGE_TYPES_MAP.get(code)
				tags = find_image_tags(item, image_type, base_url, api_key, first_only=False)
				if not tags:
					missing_types.append(image_type)
				else:
					for _, _, w, h in tags:
						if check_low_res(code, w, h, minres):
							lowres_types.append(image_type)
							break
	
			issues_fp.write(json.dumps({
				"Id": item_id,
				"missing": missing_types,
				"lowres": lowres_types
			}) + "\n")
	
			# Collect rows instead of writing now
			summary_rows.append((safe_name.lower(), item_id, safe_name, missing_types, lowres_types))

	per_item_issues: Dict[str, Dict[str, List[str]]] = {}
	with open(issues_tmp_path, 'r', encoding='utf-8') as f_issues:
		for line in f_issues:
			try:
				rec = json.loads(line)
				per_item_issues[rec["Id"]] = {
					"missing": rec.get("missing", []),
					"lowres": rec.get("lowres", []),
				}
			except Exception:
				continue

	minimal_index: List[Tuple[str, str]] = []
	with open(index_tmp_path, 'r', encoding='utf-8') as f_idx:
		for line in f_idx:
			try:
				rec = json.loads(line)
				minimal_index.append((rec["Id"], rec.get("Name", "")))
			except Exception:
				continue

	minimal_index.sort(key=lambda x: str(x[1]).lower())

	is_list_input = isinstance(items, list)
	id_to_item: Dict[str, dict] = {}
	if is_list_input:
		for it in items:
			item_id = it.get('Id')
			if item_id:
				id_to_item[item_id] = it

	with open(body_tmp_path, 'w', encoding='utf-8') as body_fp:
		for item_id, safe_name in minimal_index:
			issues = per_item_issues.get(item_id, {"missing": [], "lowres": []})
			item_missing = issues.get("missing", [])
			item_lowres = issues.get("lowres", [])

			if is_list_input:
				item = id_to_item.get(item_id, {"Id": item_id, "Name": safe_name})
			else:
				item = {"Id": item_id, "Name": safe_name}

			link_url = f"{base_url.rstrip('/')}/web/index.html#!/details?id={item['Id']}"
			body_fp.write(f'<div class="movie" id="item_{item["Id"]}"><h2 class="entry-title"><a target="_blank" href="{link_url}">{safe_name}</a></h2>\n')
			body_fp.write('<div class="image-row">\n')

			body_fp.write('<div class="left-column">\n')
			for code in left_codes:
				image_type_name = IMAGE_TYPES_MAP.get(code)
				tags = find_image_tags(item, image_type_name, base_url, api_key)
				if tags:
					for itype, url, w, h in tags:
						low = check_low_res(code, w, h, minres)
						alt_caption = f"{safe_name} - {itype} ({w}x{h})" + (" - LOW RESOLUTION" if low else "")
						body_fp.write(f'''
<div class="image-grid">
  <img src="{url}" alt="{alt_caption}" loading="lazy"
   onclick="openLightbox('{item["Id"]}', '{url}'); return false;"
   style="cursor:pointer; border:2px solid #ccc; border-radius:5px;">

  {_build_caption_html(itype, w, h, low)}
</div>''')
				else:
					body_fp.write(f'<div class="placeholder">Missing: {image_type_name}</div>\n')

			issues_lines = []
			if item_missing:
				issues_lines.append("Missing:<br>" + ", ".join(item_missing))
			if item_lowres:
				issues_lines.append("Low Resolution:<br>" + ", ".join(item_lowres))
			if issues_lines:
				body_fp.write('<div class="missing-list">' + "<br><br>".join(issues_lines) + '</div>\n')

			body_fp.write('</div>\n')  # left-column

			body_fp.write('<div class="right-column">\n')

			if 'bd' in right_codes:
				tags = find_image_tags(item, 'Backdrop', base_url, api_key, first_only=False)
				if tags:
					for itype, url, w, h in tags:
						low = check_low_res('bd', w, h, minres)
						alt_caption = f"{safe_name} - {itype} ({w}x{h})" + (" - LOW RESOLUTION" if low else "")
						body_fp.write(f'''
<div class="image-grid">
  <img src="{url}" class="banner-full" alt="{alt_caption}" loading="lazy"
   onclick="openLightbox('{item["Id"]}', '{url}'); return false;"
   style="cursor:pointer; border:2px solid #ccc; border-radius:5px;">
  {_build_caption_html(itype, w, h, low)}
</div>''')
				else:
					body_fp.write('<div class="placeholder">Missing: Backdrop</div>\n')

			if 'bn' in right_codes:
				tags = find_image_tags(item, 'Banner', base_url, api_key)
				if tags:
					for itype, url, w, h in tags:
						low = check_low_res('bn', w, h, minres)
						alt_caption = f"{safe_name} - {itype} ({w}x{h})" + (" - LOW RESOLUTION" if low else "")
						body_fp.write(f'''
<div class="image-grid">
  <img src="{url}" class="banner-full" alt="{alt_caption}" loading="lazy"
   onclick="openLightbox('{item["Id"]}', '{url}'); return false;"
   style="cursor:pointer; border:2px solid #ccc; border-radius:5px;">
  {_build_caption_html(itype, w, h, low)}
</div>''')
				else:
					body_fp.write('<div class="placeholder">Missing: Banner</div>\n')

			body_fp.write('<div class="box-row">\n')
			for code in ['b', 'br', 'd']:
				if code in right_codes:
					image_type_name = IMAGE_TYPES_MAP[code]
					tags = find_image_tags(item, image_type_name, base_url, api_key)
					if tags:
						for itype, url, w, h in tags:
							low = check_low_res(code, w, h, minres)
							alt_caption = f"{safe_name} - {itype} ({w}x{h})" + (" - LOW RESOLUTION" if low else "")
							body_fp.write(f'''
<div class="image-grid">
  <img src="{url}" alt="{alt_caption}" loading="lazy"
   onclick="openLightbox('{item["Id"]}', '{url}'); return false;"
   style="cursor:pointer; border:2px solid #ccc; border-radius:5px;">
  {_build_caption_html(itype, w, h, low)}
</div>''')
					else:
						body_fp.write(f'<div class="image-grid"><div class="placeholder">Missing: {image_type_name}</div></div>\n')
			body_fp.write('</div>\n')

			if 'l' in right_codes:
				tags = find_image_tags(item, 'Logo', base_url, api_key)
				if tags:
					for itype, url, w, h in tags:
						low = check_low_res('l', w, h, minres)
						alt_caption = f"{safe_name} - {itype} ({w}x{h})" + (" - LOW RESOLUTION" if low else "")
						body_fp.write(f'''
<div class="image-grid">
  <img src="{url}" class="logo-img" alt="{alt_caption}" loading="lazy"
   onclick="openLightbox('{item["Id"]}', '{url}'); return false;"
   style="cursor:pointer; border:2px solid #ccc; border-radius:5px;">
  {_build_caption_html(itype, w, h, low)}
</div>''')
				else:
					body_fp.write('<div class="placeholder">Missing: Logo</div>\n')

			body_fp.write('</div>\n')  # right-column

			body_fp.write('</div>\n')  # image-row
			body_fp.write('<div class="scroll-top"><a href="#top">↑ Scroll to Top</a></div>\n')
			body_fp.write('</div>\n')  # movie (border box)

	with open(output_file, 'w', encoding='utf-8') as out_fp:
		_write_html_header(out_fp, bgcolor, textcolor, tablebgcolor, library_name, timestamp)
		_write_summary_table_open(out_fp, image_types)
		# write summary rows alphabetically
		for _, item_id, safe_name, missing_types, lowres_types in sorted(summary_rows, key=lambda x: x[0]):
			_write_summary_table_row(out_fp, item_id, safe_name, image_types, missing_types, lowres_types)
		_write_summary_table_close(out_fp)
		with open(body_tmp_path, 'r', encoding='utf-8') as body_fp:
			for line in body_fp:
				out_fp.write(line)
		_write_lightbox(out_fp)
		_write_footer(out_fp)

	print(f"HTML file generated: {output_file}")

# ----------------------------------------------------------------------
# NEW: ZIP CREATION
# ----------------------------------------------------------------------

def create_zip(items, image_types: List[str], base_url: str, api_key: str,
				zip_output_file: str, library_name: str, zip_basename_overrides: Dict[str, str] | None = None):
	"""
	Create a ZIP archive with a folder per entry item, downloading image files
	for the selected image types. Supports duplicate titles by appending the
	release year instead of numbers to avoid sequel confusion.
	Resolves relative zip_output_file relative to script directory.
	"""
	if not os.path.isabs(zip_output_file):
		zip_output_file = os.path.join(BASE_DIR, zip_output_file)

	os.makedirs(os.path.dirname(zip_output_file) or '.', exist_ok=True)

	# Build map for overrides (base names, no extension)
	name_overrides = dict(DEFAULT_ZIP_BASENAMES)
	if zip_basename_overrides:
		for code, name in zip_basename_overrides.items():
			if code in name_overrides and isinstance(name, str) and name.strip():
				name_overrides[code] = name.strip()

	# Track seen names and disambiguate with years
	seen_names: Dict[str, List[str]] = {}

	with ZipFile(zip_output_file, 'w', compression=ZIP_DEFLATED) as zf:
		for item in items:
			title = _safe_name(item)
			folder = sanitize_folder_name(title)

			# Try to get the production/release year
			year = None
			if "ProductionYear" in item and item["ProductionYear"]:
				year = str(item["ProductionYear"])
			elif "PremiereDate" in item and item["PremiereDate"]:
				try:
					year = str(datetime.fromisoformat(item["PremiereDate"]).year)
				except Exception:
					pass

			# Disambiguate by year if duplicate
			if folder in seen_names:
				if year and year not in seen_names[folder]:
					folder = f"{folder} ({year})"
					seen_names[title].append(year)
				else:
					# fallback: still add a number if year missing or already used
					count = len(seen_names[folder]) + 1
					folder = f"{folder} {count}"
					seen_names[folder].append(str(count))
			else:
				# first occurrence
				if year:
					seen_names[folder] = [year]
					folder = f"{folder} ({year})"
				else:
					seen_names[folder] = ["1"]

			folder = sanitize_folder_name(folder)

			# For each selected type, fetch tags (may be multiple)
			for code in image_types:
				image_type_name = IMAGE_TYPES_MAP.get(code)
				if not image_type_name:
					continue
				tags = find_image_tags(item, image_type_name, base_url, api_key, first_only=False)
				if not tags:
					continue

				base_name = name_overrides.get(code, DEFAULT_ZIP_BASENAMES.get(code, image_type_name.lower()))
				# Append numbers to filenames ONLY when multiple images of that type
				multi = len(tags) > 1
				for idx, (_, url, _, _) in enumerate(tags, start=1):
					try:
						data, ext = stream_to_bytes(url)
						if multi:
							filename = f"{base_name}{idx:02d}{ext}"  # e.g. backdrop01, backdrop02
						else:
							filename = f"{base_name}{ext}"
						arcname = f"{folder}/{filename}"
						zf.writestr(arcname, data)
						print(f"Added: {arcname}")
					except Exception as e:
						print(f"Failed to add image for item '{title}' ({image_type_name}): {e}")

	print(f"ZIP file created: {zip_output_file}")

# ----------------------------------------------------------------------
# ORIGINAL CLI (PRESERVED + new args for timestamp & zip)
# ----------------------------------------------------------------------

if __name__ == '__main__':
	parser = argparse.ArgumentParser(description='Generate HTML gallery from Jellyfin library')
	parser.add_argument('--server', required=True)
	parser.add_argument('--apikey', required=True)
	parser.add_argument('--library', required=True)
	parser.add_argument('--output', default='gallery.html')
	parser.add_argument('--bgcolor', default='#222')
	parser.add_argument('--textcolor', default='#eee')
	parser.add_argument('--tablebgcolor', default='#333')
	parser.add_argument('--images', default='p,t,c,m,bd,bn,b,br,d,l')
	parser.add_argument('--minres', default='', help='Semicolon-separated list like "bd:3840x2160;p:2000x3000"')
	# Optional explicit timestamp (used by app.py to pass TZ-aware string)
	parser.add_argument('--timestamp', default=None, help='Optional timestamp string to embed in HTML')
	# NEW: ZIP creation options
	parser.add_argument('--zip-output', default=None, help='If provided, create ZIP at this path instead of/generally in addition to HTML')
	parser.add_argument('--zipnames', default=None, help='JSON of code->basename (no extension) overrides for ZIP creation')

	args = parser.parse_args()

	image_types = [c for c in args.images.split(',') if c in IMAGE_TYPES_MAP]
	minres = parse_minres_arg(args.minres)

	try:
		user_id = get_first_user_id(args.server, args.apikey)
		library_id, library_type = get_library_id(args.server, args.apikey, user_id, args.library)
		if not library_id:
			print(f"Library '{args.library}' not found for user.")
			sys.exit(1)

		items = get_library_items(args.server, args.apikey, user_id, library_id, library_type)

		# ZIP mode (if requested). This is additive; you can request only ZIP (app.py does) or do both.
		if args.zip_output:
			try:
				overrides = json.loads(args.zipnames) if args.zipnames else {}
			except Exception:
				overrides = {}
			create_zip(items, image_types, args.server, args.apikey, args.zip_output, args.library, overrides)

		# HTML generation (always allowed; app.py decides which to run)
		timestamp = args.timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
		generate_html(items, image_types, args.server, args.apikey, args.output,
					  args.bgcolor, args.textcolor, args.tablebgcolor,
					  library_type, args.library, timestamp, minres)

	except requests.HTTPError as e:
		print(f"HTTP error: {e}")
		sys.exit(1)
	except requests.RequestException as e:
		print(f"Request failed: {e}")
		sys.exit(1)
	except Exception as e:
		print(f"Error: {e}")
		sys.exit(1)
