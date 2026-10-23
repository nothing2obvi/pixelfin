FROM python:3.12-slim

ARG PIXELFIN_VERSION=dev
ENV PIXELFIN_VERSION=${PIXELFIN_VERSION}

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py generate_html.py restore.py fresh_jellyfin.py fresh_state.py image_editor.py /app/
COPY assets /app/assets
COPY templates /app/templates

RUN mkdir -p /app/data /app/output

EXPOSE 1280

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
	CMD ["python", "-c", "import sys, requests; session = requests.Session(); session.trust_env = False; response = session.get('http://127.0.0.1:1280/health', timeout=3, allow_redirects=False); sys.exit(0 if response.status_code == 200 else 1)"]

CMD ["gunicorn", "--bind", "0.0.0.0:1280", "--timeout", "300", "--threads", "4", "app:app"]
