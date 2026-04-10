FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py generate_html.py restore.py /app/
COPY assets /app/assets
COPY templates /app/templates

RUN mkdir -p /app/data /app/output

EXPOSE 1280

# Change the CMD line to include the timeout - needed for large collections
CMD ["gunicorn", "--bind", "0.0.0.0:1280", "--timeout", "300", "app:app"]
