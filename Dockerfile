FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py generate_html.py restore.py /app/
COPY assets /app/assets
COPY templates /app/templates

RUN mkdir -p /app/data /app/output

EXPOSE 1280

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:1280", "app:app"]