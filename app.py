from flask import Flask, request, render_template, Response, send_from_directory, redirect, url_for
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
import os
import json

HISTORY_FILE = "data/history.json"
# Ensure file exists
if not os.path.exists(HISTORY_FILE):
	open(HISTORY_FILE, "w").close()  # creates empty file

def load_history():
	if not os.path.exists(HISTORY_FILE):
		open(HISTORY_FILE, "w").close()
		return {}
	with open(HISTORY_FILE, "r") as f:
		content = f.read().strip()
		if not content:  # empty file
			return {}
		try:
			return json.loads(content)
		except Exception:
			return {}

app = Flask(__name__, template_folder="templates")
BASE_OUTPUT_DIR = "output"
ASSETS_DIR = "assets"

os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
app.logger.addHandler(logging.StreamHandler())

IMAGE_TYPE_OPTIONS = {
	'p': 'Primary', 'c': 'ClearArt', 'bd': 'Backdrop', 'bn': 'Banner',
	'b': 'Box', 'br': 'BoxRear', 'd': 'Disc', 'l': 'Logo', 'm': 'Menu', 't': 'Thumb'
}

DEFAULT_ZIP_BASENAMES = {
	'p': 'cover', 't': 'thumbnail', 'bd': 'backdrop', 'c': 'clearart',
	'bn': 'banner', 'b': 'box', 'br': 'boxrear', 'd': 'disc', 'l': 'logo', 'm': 'menu'
}

def load_pixelfin_base64(filename):
	path = os.path.join(ASSETS_DIR, filename)
	if os.path.exists(path):
		with open(path, "rb") as f:
			return base64.b64encode(f.read()).decode('utf-8')
	return ""

PIXELFIN_BASE64 = load_pixelfin_base64("Pixelfin.png")
PIXELFIN_FAVICON_BASE64 = load_pixelfin_base64("Pixelfin_Favicon.png")

# ----------------- History Helpers -----------------
def load_history():
	if os.path.exists(HISTORY_FILE):
		if os.path.isdir(HISTORY_FILE):
			shutil.rmtree(HISTORY_FILE)
			with open(HISTORY_FILE, "w") as f: f.write("{}")
	else:
		with open(HISTORY_FILE, "w") as f: f.write("{}")
	with open(HISTORY_FILE, "r") as f:
		try:
			return json.load(f)
		except Exception:
			return {'servers': [], 'libraries': [], 'library_settings': {}, 'last_used': {}}

def save_history(server, library, settings):
			history = load_history()
			
			if server not in history.get('servers', []):
				history.setdefault('servers', []).append(server)
			
			if library not in history.get('libraries', []):
				history.setdefault('libraries', []).append(library)
			
			history.setdefault('library_settings', {})[library] = settings
			
			# ✅ Save last-used settings, including colors
			history['last_used'] = {
				'server': server,
				'apikey': settings.get('apikey', ''),
				'images': settings.get('images', list(IMAGE_TYPE_OPTIONS.keys())),
				'minres': settings.get('minres', {}),
				'zipnames': settings.get('zipnames', {}),
				'bgcolor': settings.get('bgcolor', '#000000'),
				'textcolor': settings.get('textcolor', '#ffffff'),
				'tablebgcolor': settings.get('tablebgcolor', '#000000')
			}
			
			with open(HISTORY_FILE, 'w') as f:
				json.dump(history, f, indent=2)


def list_generated_htmls():
	result = {}
	history = load_history()
	if not os.path.exists(BASE_OUTPUT_DIR):
		return result
	for folder in sorted(os.listdir(BASE_OUTPUT_DIR)):
		lib_folder = os.path.join(BASE_OUTPUT_DIR, folder)
		if os.path.isdir(lib_folder):
			files = []
			for f in sorted(os.listdir(lib_folder), reverse=True):
				if f.endswith((".html", ".zip")):
					files.append({
						"filename": f,
						"name": f,
						"path": f"/output/{folder}/{quote(f)}",
						"folder": folder
					})
			if files:
				display_name = next((lib for lib in history.get('libraries', []) if lib.replace(" ", "") == folder), folder)
				result[display_name] = files
	return result

def now_in_tz():
	tzname = os.environ.get("TZ")
	try:
		if tzname:
			return datetime.now(ZoneInfo(tzname))
		return datetime.now().astimezone()
	except Exception:
		return datetime.now()

# ----------------- Routes -----------------
@app.route("/", methods=["GET", "POST"])
def index():
	history = load_history()
	last_used = history.get('last_used', {})
	selected = {
		'server': last_used.get('server', ''),
		'library': '',
		'bgcolor': last_used.get('bgcolor', '#000000'),
		'textcolor': last_used.get('textcolor', '#ffffff'),
		'tablebgcolor': last_used.get('tablebgcolor', '#000000'),
		'images': last_used.get('images', list(IMAGE_TYPE_OPTIONS.keys())),
		'apikey': last_used.get('apikey', ''),
		'minres': last_used.get('minres', {}),
		'zipnames': last_used.get('zipnames', {})
	}

	if request.method == "POST" or request.args.get("library"):
		server = request.form.get("server") or selected['server']
		library = request.form.get("library") or request.args.get('library') or ''
		lib_settings = history.get('library_settings', {}).get(library, {})
		selected.update({
			'server': server or lib_settings.get('server', ''),
			'library': library,
			'bgcolor': request.form.get("bgcolor", lib_settings.get('bgcolor', '#000000')),
			'textcolor': request.form.get("textcolor", lib_settings.get('textcolor', '#ffffff')),
			'tablebgcolor': request.form.get("tablebgcolor", lib_settings.get('tablebgcolor', '#000000')),
			'images': request.form.getlist("images") or lib_settings.get('images', list(IMAGE_TYPE_OPTIONS.keys())),
			'apikey': request.form.get("apikey", lib_settings.get('apikey', last_used.get('apikey',''))),
			'minres': lib_settings.get('minres', last_used.get('minres', {})),
			'zipnames': lib_settings.get('zipnames', last_used.get('zipnames', {}))
		})

	if request.method == "POST":
		action = request.form.get("action", "html")
		server = selected['server']
		library = selected['library']
		apikey = selected['apikey']
		bgcolor = selected['bgcolor']
		textcolor = selected['textcolor']
		tablebgcolor = selected['tablebgcolor']
		selected_images = selected['images']

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
			val = request.form.get(f"zipname_{code}", "").strip()
			if val:
				zipnames[code] = val

		save_history(server, library, {
			'apikey':apikey,
			'bgcolor':bgcolor,
			'textcolor':textcolor,
			'tablebgcolor':tablebgcolor,
			'images':selected_images,
			'minres':minres,
			'zipnames':zipnames
		})

		safe_library = library
		lib_folder = os.path.join(BASE_OUTPUT_DIR, safe_library)
		os.makedirs(lib_folder, exist_ok=True)

		now = now_in_tz()
		timestamp_file = now.strftime("%Y-%m-%d_%H-%M-%S")
		timestamp_html = now.strftime("%Y-%m-%d %H:%M:%S")
		log_queue = queue.Queue()

		# ---------- ZIP ----------
		if action == "zip":
			zip_path = os.path.join(lib_folder, f"{timestamp_file} - {library}.zip")
			def run_create_zip():
				args = ["python","generate_html.py","--server",server,"--apikey",apikey,
						"--library",library,"--images",','.join(selected_images),
						"--zip-output",zip_path,"--zipnames",json.dumps(zipnames)]
				proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
				for line in proc.stdout: log_queue.put(line)
				proc.wait()
				log_queue.put(None)
			threading.Thread(target=run_create_zip).start()

			def generate_zip_stream():
				yield f"<html><head><title>Creating ZIP</title><link rel='icon' type='image/png' href='data:image/png;base64,{PIXELFIN_FAVICON_BASE64}' />"
				yield "<style>pre{overflow:auto;max-height:500px;background:#111;color:#0f0;padding:10px;}</style></head><body><pre id='log'>\n"
				while True:
					line = log_queue.get()
					if line is None: break
					yield line
				yield f"\n</pre><h3>ZIP created!</h3><a href='/output/{quote(safe_library)}/{quote(f'{timestamp_file} - {library}.zip')}'>Download ZIP</a><br><br>"
				yield "<form action='/' method='get'><input type='submit' value='Back to Main Page' /></form>"
				yield "<script>var pre=document.getElementById('log');pre.scrollTop=pre.scrollHeight;</script></body></html>"
			return Response(generate_zip_stream(), mimetype='text/html')

		# ---------- HTML ----------
		output_file = os.path.join(lib_folder, f"{timestamp_file} - {library}.html")
		def run_generate_html():
			args = ["python","generate_html.py","--server",server,"--apikey",apikey,
					"--library",library,"--output",output_file,"--bgcolor",bgcolor,
					"--textcolor",textcolor,"--tablebgcolor",tablebgcolor,"--images",','.join(selected_images)]
			if minres:
				minres_str = ";".join([f"{code}:{w}x{h}" for code,(w,h) in minres.items()])
				args += ["--minres", minres_str]
			args += ["--timestamp", timestamp_html]
			proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
			for line in proc.stdout: log_queue.put(line)
			proc.wait()
			log_queue.put(None)
		threading.Thread(target=run_generate_html).start()

		def generate():
			yield f"<html><head><title>HTML Generated!</title><link rel='icon' type='image/png' href='data:image/png;base64,{PIXELFIN_FAVICON_BASE64}' />"
			yield "<style>pre{overflow:auto;max-height:500px;background:#111;color:#0f0;padding:10px;}</style></head><body><pre id='log'>\n"
			while True:
				line = log_queue.get()
				if line is None: break
				yield line
			yield "\n</pre>"

			# inject pixelfin logo
			if PIXELFIN_BASE64 and os.path.exists(output_file):
				with open(output_file,"r",encoding="utf-8") as f: content=f.read()
				content = content.replace("<head>", f"<head>\n<link rel='icon' type='image/png' href='data:image/png;base64,{PIXELFIN_FAVICON_BASE64}' />",1)
				content = content.replace("<body>", f"<body>\n<img src='data:image/png;base64,{PIXELFIN_BASE64}' style='max-width:300px;' />",1)
				with open(output_file,"w",encoding="utf-8") as f: f.write(content)

			html_filename_url = quote(f"{timestamp_file} - {library}.html")
			yield f"<h3>HTML generated!</h3><a href='/output/{quote(safe_library)}/{html_filename_url}' target='_blank'>View</a> | "
			yield f"<a href='/download/{quote(safe_library)}/{html_filename_url}'>Download (embedded)</a><br><br>"
			yield "<form action='/' method='get'><input type='submit' value='Back to Main Page' /></form>"
			yield "<script>var pre=document.getElementById('log');pre.scrollTop=pre.scrollHeight;</script></body></html>"
			return
		return Response(generate(), mimetype='text/html')

	return render_template(
		"form.html",
		image_types=IMAGE_TYPE_OPTIONS,
		generated=list_generated_htmls(),
		history=history,
		selected=selected,
		pixelfin=PIXELFIN_BASE64,
		default_zip_basenames=DEFAULT_ZIP_BASENAMES
	)

@app.route("/output/<library>/<filename>")
def serve_output(library, filename):
	return send_from_directory(os.path.join(BASE_OUTPUT_DIR, library), filename)

@app.route("/delete/<library>/<filename>")
def delete_file(library, filename):
	file_path = os.path.join(BASE_OUTPUT_DIR, library, filename)
	if os.path.exists(file_path): os.remove(file_path)
	lib_folder = os.path.join(BASE_OUTPUT_DIR, library)
	if os.path.exists(lib_folder) and not os.listdir(lib_folder): os.rmdir(lib_folder)
	return redirect(url_for('index'))

@app.route("/download/<library>/<filename>")
def download_embedded(library, filename):
	file_path = os.path.join(BASE_OUTPUT_DIR, library, filename)
	if not os.path.exists(file_path): return "File not found", 404
	with open(file_path,"r",encoding="utf-8") as f: html=f.read()
	def embed_img(match):
		url = match.group(1)
		try:
			if url.startswith("data:"): return match.group(0)
			resp = requests.get(url); resp.raise_for_status()
			img_data = base64.b64encode(resp.content).decode('utf-8')
			ext = url.split('.')[-1].split('?')[0].lower()
			if ext not in ['jpg','jpeg','png','gif','webp','bmp']: ext='png'
			return f'<img src="data:image/{ext};base64,{img_data}" />'
		except Exception as e: app.logger.error(f"Failed to embed image {url}: {e}"); return match.group(0)
	html_embedded = re.sub(r'<img\s+[^>]*src="([^"]+)"', embed_img, html)
	return Response(html_embedded,mimetype='text/html',headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.route("/assets/<filename>")
def serve_assets(filename):
	return send_from_directory(ASSETS_DIR, filename)

if __name__ == "__main__":
	app.run(host="0.0.0.0", port=1280)
