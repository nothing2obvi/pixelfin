from flask import Flask, request, render_template_string, Response, send_from_directory, redirect, url_for
import subprocess
import os
import logging
import threading
import queue
import json
from datetime import datetime
import base64
import re
import requests
import shutil

app = Flask(__name__)
BASE_OUTPUT_DIR = "output"
ASSETS_DIR = "assets"
HISTORY_FILE = "history.json"

os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
app.logger.addHandler(logging.StreamHandler())

IMAGE_TYPE_OPTIONS = {
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

def load_pixelfin_base64():
	pix_path = os.path.join(ASSETS_DIR, "Pixelfin.png")
	if os.path.exists(pix_path):
		with open(pix_path, "rb") as f:
			return base64.b64encode(f.read()).decode('utf-8')
	return ""

PIXELFIN_BASE64 = load_pixelfin_base64()

FORM_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Pixelfin</title>
<link rel="icon" type="image/png" href="/assets/Pixelfin_Favicon.png" />
</head>
<body>
{% if pixelfin %}
<img src="data:image/png;base64,{{ pixelfin }}" style="max-width:300px;" />
<br><br>
{% endif %}
<h2>Generate HTML</h2>
<form method=post>
  Server URL: 
  <input name="server" list="servers" value="{{selected.server}}" required>
  <datalist id="servers">
	{% for s in history.servers %}
	  <option value="{{s}}">
	{% endfor %}
  </datalist>
  <br><br>

  Library Name:
  <input name="library" list="libraries" value="{{selected.library}}" required>
  <datalist id="libraries">
	{% for l in history.libraries %}
	  <option value="{{l}}">
	{% endfor %}
  </datalist>
  <br><br>

  Background Color: <input type=color name=bgcolor value="{{selected.bgcolor}}"><br><br>
  Text Color: <input type=color name=textcolor value="{{selected.textcolor}}"><br><br>
  Table Background Color: <input type=color name=tablebgcolor value="{{selected.tablebgcolor}}"><br><br>
  <b>Image Types:</b><br>
  {% for code, label in image_types.items() %}
	<input type=checkbox name=images value="{{ code }}" {% if code in selected.images %}checked{% endif %}> {{ label }}<br>
  {% endfor %}
  <br>
  API Key: <input type=text name=apikey value="{{selected.apikey}}" required><br><br>
  <input type=submit value="Generate">
</form>

<h2>Previously Generated HTMLs</h2>
{% if generated %}
  {% for library, files in generated.items() %}
	<h3>{{ library }}</h3>
	<ul>
	  {% for f in files %}
		<li>
		  {{f.name}} - 
		  <a href="{{ f.path }}" target="_blank">View</a> | 
		  <a href="{{ url_for('download_embedded', library=f.folder, filename=f.filename) }}">Download</a> | 
		  <a href="{{ url_for('delete_file', library=f.folder, filename=f.filename) }}" onclick="return confirm('Delete this file?');">Delete</a>
		</li>
	  {% endfor %}
	</ul>
  {% endfor %}
{% else %}
  <p>No HTML generated yet.</p>
{% endif %}
</body>
</html>
"""

def load_history():
	if os.path.exists(HISTORY_FILE):
		if os.path.isdir(HISTORY_FILE):
			shutil.rmtree(HISTORY_FILE)
			with open(HISTORY_FILE, "w") as f:
				f.write("{}")
	else:
		with open(HISTORY_FILE, "w") as f:
			f.write("{}")

	with open(HISTORY_FILE, "r") as f:
		try:
			return json.load(f)
		except Exception:
			return {'servers': [], 'libraries': [], 'library_settings': {}, 'last_used': {}}

def save_history(server, library, settings):
	history = load_history()
	if server not in history['servers']:
		history['servers'].append(server)
	if library not in history['libraries']:
		history['libraries'].append(library)
	history.setdefault('library_settings', {})[library] = settings
	history['last_used'] = {
		'server': server,
		'apikey': settings['apikey'],
		'images': settings['images']
	}
	with open(HISTORY_FILE, 'w') as f:
		json.dump(history, f)

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
				if f.endswith(".html"):
					files.append({
						"name": f.replace(".html", "").replace("_", " "),
						"filename": f,
						"path": f"/output/{folder}/{f}",
						"folder": folder
					})
			if files:
				display_name = next((lib for lib in history.get('libraries', []) if lib.replace(" ", "") == folder), folder)
				result[display_name] = files
	return result

@app.route("/", methods=["GET", "POST"])
def index():
	history = load_history()
	last_used = history.get('last_used', {})
	selected = {
		'server': last_used.get('server',''),
		'library': '',
		'bgcolor': '#000000',
		'textcolor': '#ffffff',
		'tablebgcolor': '#000000',
		'images': last_used.get('images', list(IMAGE_TYPE_OPTIONS.keys())),
		'apikey': last_used.get('apikey','')
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
			'apikey': request.form.get("apikey", lib_settings.get('apikey', last_used.get('apikey','')))
		})

	if request.method == "POST":
		server = selected['server']
		library = selected['library']
		apikey = selected['apikey']
		bgcolor = selected['bgcolor']
		textcolor = selected['textcolor']
		tablebgcolor = selected['tablebgcolor']
		selected_images = selected['images']

		save_history(server, library, {
			'apikey': apikey,
			'bgcolor': bgcolor,
			'textcolor': textcolor,
			'tablebgcolor': tablebgcolor,
			'images': selected_images
		})

		safe_library = library.replace(" ", "")
		lib_folder = os.path.join(BASE_OUTPUT_DIR, safe_library)
		os.makedirs(lib_folder, exist_ok=True)

		timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
		output_file = os.path.join(lib_folder, f"{timestamp}.html")

		log_queue = queue.Queue()

		def run_generate_html():
			proc = subprocess.Popen(
				[
					"python", "generate_html.py",
					"--server", server,
					"--apikey", apikey,
					"--library", library,
					"--output", output_file,
					"--bgcolor", bgcolor,
					"--textcolor", textcolor,
					"--tablebgcolor", tablebgcolor,
					"--images", ','.join(selected_images)
				],
				stdout=subprocess.PIPE,
				stderr=subprocess.STDOUT,
				text=True
			)
			for line in proc.stdout:
				log_queue.put(line)
			proc.wait()
			log_queue.put(None)

		threading.Thread(target=run_generate_html).start()

		def generate():
			yield "<pre>\n"
			while True:
				line = log_queue.get()
				if line is None:
					break
				yield line
			yield "\n</pre>\n"

			if PIXELFIN_BASE64:
				with open(output_file, "r", encoding="utf-8") as f:
					content = f.read()
				content = content.replace("<body>", f"<body>\n<img src='data:image/png;base64,{PIXELFIN_BASE64}' style='max-width:300px;' />", 1)
				with open(output_file, "w", encoding="utf-8") as f:
					f.write(content)

			yield f"""
			<h3>HTML generated!</h3>
			<a href='/output/{safe_library}/{timestamp}.html' target='_blank'>View</a> | 
			<a href='/download/{safe_library}/{timestamp}.html'>Download (embedded)</a><br><br>
			<form action='/' method='get'>
				<input type='submit' value='Back to Main Page' />
			</form>
			"""

		return Response(generate(), mimetype='text/html')

	generated_list = list_generated_htmls()
	return render_template_string(FORM_HTML, image_types=IMAGE_TYPE_OPTIONS, generated=generated_list, history=history, selected=selected, pixelfin=PIXELFIN_BASE64)

@app.route("/output/<library>/<filename>")
def serve_output(library, filename):
	return send_from_directory(os.path.join(BASE_OUTPUT_DIR, library), filename)

@app.route("/delete/<library>/<filename>")
def delete_file(library, filename):
	file_path = os.path.join(BASE_OUTPUT_DIR, library, filename)
	if os.path.exists(file_path):
		os.remove(file_path)
	lib_folder = os.path.join(BASE_OUTPUT_DIR, library)
	if os.path.exists(lib_folder) and not os.listdir(lib_folder):
		os.rmdir(lib_folder)
	return redirect(url_for('index'))

@app.route("/download/<library>/<filename>")
def download_embedded(library, filename):
	file_path = os.path.join(BASE_OUTPUT_DIR, library, filename)
	if not os.path.exists(file_path):
		return "File not found", 404

	with open(file_path, "r", encoding="utf-8") as f:
		html = f.read()

	def embed_img(match):
		url = match.group(1)
		try:
			if url.startswith("data:"):
				return match.group(0)
			resp = requests.get(url)
			resp.raise_for_status()
			img_data = base64.b64encode(resp.content).decode('utf-8')
			ext = url.split('.')[-1].split('?')[0].lower()
			if ext not in ['jpg','jpeg','png','gif','webp','bmp']:
				ext = 'png'
			return f'<img src="data:image/{ext};base64,{img_data}" />'
		except Exception as e:
			app.logger.error(f"Failed to embed image {url}: {e}")
			return match.group(0)

	html_embedded = re.sub(r'<img\s+[^>]*src="([^"]+)"', embed_img, html)

	return Response(
		html_embedded,
		mimetype='text/html',
		headers={"Content-Disposition": f"attachment; filename={filename}"}
	)

@app.route("/assets/<filename>")
def serve_assets(filename):
	return send_from_directory(ASSETS_DIR, filename)

if __name__ == "__main__":
	app.run(host="0.0.0.0", port=1280)
