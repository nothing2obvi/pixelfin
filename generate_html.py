#!/usr/bin/env python3
import requests
import argparse
import sys
import os
import warnings
from urllib.parse import urljoin
from io import BytesIO
from PIL import Image
from datetime import datetime

warnings.simplefilter('ignore', Image.DecompressionBombWarning)

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

# Columns and display order
# Left: Primary → Thumb → ClearArt → Menu (all full width)
LEFT_TYPES = ['p', 't', 'c', 'm']
# Right: Backdrop (full) → Banner (full) → Box → BoxRear → Disc (row, 1/3 each) → Logo (60% width, left)
RIGHT_TYPES = ['bd', 'bn', 'b', 'br', 'd', 'l']

def get_first_user_id(base_url, api_key):
	url = urljoin(base_url.rstrip('/') + '/', 'Users')
	headers = {'X-Emby-Token': api_key}
	resp = requests.get(url, headers=headers)
	resp.raise_for_status()
	for user in resp.json():
		if not user.get('IsHidden', False):
			return user['Id']
	raise Exception("No enabled user found")

def get_library_id(base_url, api_key, user_id, library_name):
	url = urljoin(base_url.rstrip('/') + '/', f'Users/{user_id}/Views')
	headers = {'X-Emby-Token': api_key}
	resp = requests.get(url, headers=headers)
	resp.raise_for_status()
	for item in resp.json()['Items']:
		if item['Name'].lower() == library_name.lower():
			return item['Id'], item.get('CollectionType', '')
	return None, None

def get_library_items(base_url, api_key, user_id, library_id, library_type):
	items = []
	headers = {'X-Emby-Token': api_key}
	start_index = 0
	limit = 100
	lib_type_lower = (library_type or '').lower()

	while True:
		url = urljoin(
			base_url.rstrip('/') + '/',
			f'Users/{user_id}/Items?ParentId={library_id}&Recursive=false&StartIndex={start_index}&Limit={limit}'
		)
		resp = requests.get(url, headers=headers)
		resp.raise_for_status()
		data = resp.json()
		for item in data.get('Items', []):
			type_lower = (item.get('Type') or '').lower()
			if lib_type_lower == 'series' and type_lower != 'series':
				continue
			elif lib_type_lower == 'movie' and type_lower != 'movie':
				continue
			elif lib_type_lower == 'music':
				items.append(item)
				continue
			elif lib_type_lower == 'musicvideos':
				if type_lower in ('artist', 'musicvideoalbum', 'folder'):
					items.append(item)
				continue
			items.append(item)
		if len(data.get('Items', [])) < limit:
			break
		start_index += limit
	return items

def get_image_resolution(url):
	try:
		resp = requests.get(url)
		resp.raise_for_status()
		img = Image.open(BytesIO(resp.content))
		return img.size
	except Exception:
		return (0, 0)

def find_image_tags(item, image_type, base_url, api_key, first_only=False):
	"""
	Returns list of tuples: (ImageTypeName, url, width, height)
	"""
	image_tags_dict = item.get('ImageTags', {}) or {}
	tags = []
	# Tagged images (supports multiple)
	for key, tag in image_tags_dict.items():
		key_lower = (key or '').lower()
		if key_lower.startswith((image_type or '').lower()):
			url = f"{base_url.rstrip('/')}/Items/{item['Id']}/Images/{image_type}?tag={tag}&api_key={api_key}"
			width, height = get_image_resolution(url)
			tags.append((image_type, url, width, height))
			if first_only:
				return tags
	# Fallback untagged endpoint
	if not tags:
		url = f"{base_url.rstrip('/')}/Items/{item['Id']}/Images/{image_type}?api_key={api_key}"
		width, height = get_image_resolution(url)
		if width != 0:
			tags.append((image_type, url, width, height))
	return tags

def generate_html(items, image_types, base_url, api_key, output_file, bgcolor, textcolor, tablebgcolor, library_type, library_name, timestamp):
	html = [f'''<html>
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
/* Missing list pinned to bottom of left column */
.missing-list {{ color:red; font-weight:bold; text-align:center; margin-top:auto; }}
/* Visual placeholder boxes for missing images */
.placeholder {{ border:2px dashed red; border-radius:5px; color:red; font-weight:bold; display:flex; align-items:center; justify-content:center; height:150px; }}
a {{ color: {textcolor}; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.backlink {{ margin-bottom: 20px; }}
.scroll-top {{ text-align:center; margin-top:10px; }}
.entry-title {{ margin-bottom:15px; }}
.resolution {{ font-size:14px; opacity:0.9; }}
</style>
</head>
<body>
<div class="backlink" id="top"><a href="/">← Back to Main Page</a></div>
<h1>{library_name}</h1>
<p>Generated: {timestamp}</p>
''']

	items.sort(key=lambda x: str(x.get('Name', '')).lower())

	# Summary Table
	html.append('<h2>Missing Image Types Summary</h2>')
	html.append('<table><tr><th>Item Name</th>')
	for code in image_types:
		html.append(f'<th>{IMAGE_TYPES_MAP.get(code, code)}</th>')
	html.append('</tr>')

	missing_summary = {}
	for item in items:
		missing_types = []
		for code in image_types:
			image_type = IMAGE_TYPES_MAP.get(code)
			tags = find_image_tags(item, image_type, base_url, api_key, first_only=True)
			if not tags:
				missing_types.append(image_type)
		missing_summary[item['Id']] = missing_types
		safe_name = str(item.get("Name", ""))
		html.append(f'<tr><td><a href="#item_{item["Id"]}">{safe_name}</a></td>')
		for code in image_types:
			html.append(f'<td>{"Yes" if IMAGE_TYPES_MAP.get(code) in missing_types else ""}</td>')
		html.append('</tr>')
	html.append('</table>')

	left_codes = [c for c in LEFT_TYPES if c in image_types]
	right_codes = [c for c in RIGHT_TYPES if c in image_types]

	for item in items:
		link_url = f"{base_url.rstrip('/')}/web/index.html#!/details?id={item['Id']}"
		safe_name = str(item.get("Name", ""))
		html.append(f'<div class="movie" id="item_{item["Id"]}"><h2 class="entry-title"><a target="_blank" href="{link_url}">{safe_name}</a></h2>')
		html.append('<div class="image-row">')

		# LEFT COLUMN (1/3 width)
		html.append('<div class="left-column">')
		all_missing = []
		for code in left_codes + right_codes:
			image_type_name = IMAGE_TYPES_MAP.get(code)
			tags = find_image_tags(item, image_type_name, base_url, api_key)
			if not tags:
				all_missing.append(image_type_name)

		for code in left_codes:
			image_type_name = IMAGE_TYPES_MAP.get(code)
			tags = find_image_tags(item, image_type_name, base_url, api_key)
			if tags:
				for itype, url, w, h in tags:
					caption = f"{safe_name} - {itype} ({w}x{h})"
					html.append(f'''
<div class="image-grid">
  <a href="#lightbox" onclick="openLightbox('{item["Id"]}', this.querySelector('img').src); return false;">
	<img src="{url}" alt="{caption}" loading="lazy">
  </a>
  <div class="resolution">{itype} {w}x{h}</div>
</div>''')
			else:
				html.append(f'<div class="placeholder">Missing: {image_type_name}</div>')

		if all_missing:
			html.append('<div class="missing-list">Missing:<br>' + ", ".join(all_missing) + '</div>')
		html.append('</div>')  # left-column

		# RIGHT COLUMN (2/3 width)
		html.append('<div class="right-column">')

		# Backdrop (full width)
		if 'bd' in right_codes:
			tags = find_image_tags(item, 'Backdrop', base_url, api_key)
			if tags:
				for itype, url, w, h in tags:
					caption = f"{safe_name} - {itype} ({w}x{h})"
					html.append(f'''
<div class="image-grid">
  <a href="#lightbox" onclick="openLightbox('{item["Id"]}', this.querySelector('img').src); return false;">
	<img src="{url}" class="banner-full" alt="{caption}" loading="lazy">
  </a>
  <div class="resolution">{itype} {w}x{h}</div>
</div>''')
			else:
				html.append('<div class="placeholder">Missing: Backdrop</div>')

		# Banner (full width)
		if 'bn' in right_codes:
			tags = find_image_tags(item, 'Banner', base_url, api_key)
			if tags:
				for itype, url, w, h in tags:
					caption = f"{safe_name} - {itype} ({w}x{h})"
					html.append(f'''
<div class="image-grid">
  <a href="#lightbox" onclick="openLightbox('{item["Id"]}', this.querySelector('img').src); return false;">
	<img src="{url}" class="banner-full" alt="{caption}" loading="lazy">
  </a>
  <div class="resolution">{itype} {w}x{h}</div>
</div>''')
			else:
				html.append('<div class="placeholder">Missing: Banner</div>')

		# Box, BoxRear, Disc in a single row (each ~1/3 width)
		html.append('<div class="box-row">')
		for code in ['b', 'br', 'd']:
			if code in right_codes:
				image_type_name = IMAGE_TYPES_MAP[code]
				tags = find_image_tags(item, image_type_name, base_url, api_key)
				if tags:
					for itype, url, w, h in tags:
						caption = f"{safe_name} - {itype} ({w}x{h})"
						html.append(f'''
<div class="image-grid">
  <a href="#lightbox" onclick="openLightbox('{item["Id"]}', this.querySelector('img').src); return false;">
	<img src="{url}" alt="{caption}" loading="lazy">
  </a>
  <div class="resolution">{itype} {w}x{h}</div>
</div>''')
				else:
					html.append(f'<div class="image-grid"><div class="placeholder">Missing: {image_type_name}</div></div>')
		html.append('</div>')  # .box-row

		# Logo (60% width, left-aligned)
		if 'l' in right_codes:
			tags = find_image_tags(item, 'Logo', base_url, api_key)
			if tags:
				for itype, url, w, h in tags:
					caption = f"{safe_name} - {itype} ({w}x{h})"
					html.append(f'''
<div class="image-grid">
  <a href="#lightbox" onclick="openLightbox('{item["Id"]}', this.querySelector('img').src); return false;">
	<img src="{url}" class="logo-img" alt="{caption}" loading="lazy">
  </a>
  <div class="resolution">{itype} {w}x{h}</div>
</div>''')
			else:
				html.append('<div class="placeholder">Missing: Logo</div>')

		html.append('</div>')  # right-column

		html.append('</div>')  # image-row
		html.append('<div class="scroll-top"><a href="#top">↑ Scroll to Top</a></div>')
		html.append('</div>')  # movie

	# LIGHTBOX (restored)
	html.append('''
<div id="lightbox" class="lightbox" onclick="clickOutside(event)">
  <div class="lightbox-content">
	<div class="lightbox-caption" id="lightbox-caption"></div>
	<img id="lightbox-img" src="" onclick="closeLightbox()">
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

function showImage(){
  if(!currentImages.length) return;
  document.getElementById('lightbox-img').src = currentImages[currentIndex].src;
  document.getElementById('lightbox-caption').innerText = currentImages[currentIndex].caption;
}

function closeLightbox(){
  document.getElementById('lightbox').style.display='none';
  currentImages=[];
  currentIndex=0;
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

	html.append('</body></html>')

	os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)
	with open(output_file, 'w', encoding='utf-8') as f:
		f.write('\n'.join(html))
	print(f"HTML file generated: {output_file}")

if __name__ == '__main__':
	parser = argparse.ArgumentParser(description='Generate HTML gallery from Jellyfin library')
	parser.add_argument('--server', required=True)
	parser.add_argument('--apikey', required=True)
	parser.add_argument('--library', required=True)
	parser.add_argument('--output', default='gallery.html')
	parser.add_argument('--bgcolor', default='#222')
	parser.add_argument('--textcolor', default='#eee')
	parser.add_argument('--tablebgcolor', default='#333')
	# Default order matches requested layout; no duplicate "box"
	parser.add_argument('--images', default='p,t,c,m,bd,bn,b,br,d,l')
	args = parser.parse_args()

	image_types = args.images.split(',')

	try:
		user_id = get_first_user_id(args.server, args.apikey)
		library_id, library_type = get_library_id(args.server, args.apikey, user_id, args.library)
		if not library_id:
			print(f"Library '{args.library}' not found for user.")
			sys.exit(1)
		items = get_library_items(args.server, args.apikey, user_id, library_id, library_type)
		timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
		generate_html(items, image_types, args.server, args.apikey, args.output,
					  args.bgcolor, args.textcolor, args.tablebgcolor,
					  library_type, args.library, timestamp)
	except requests.HTTPError as e:
		print(f"HTTP error: {e}")
		sys.exit(1)
	except requests.RequestException as e:
		print(f"Request failed: {e}")
		sys.exit(1)
	except Exception as e:
		print(f"Error: {e}")
		sys.exit(1)
