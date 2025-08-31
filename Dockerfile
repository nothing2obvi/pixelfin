# Use minimal Python image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY app.py generate_html.py /app/
COPY assets /app/assets
COPY templates /app/templates


# Expose the port
EXPOSE 1280

# Start the app
CMD ["python", "app.py"]
