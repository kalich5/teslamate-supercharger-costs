FROM python:3.12-slim

LABEL org.opencontainers.image.title="teslamate-supercharger-costs" \
      org.opencontainers.image.description="Imports real Supercharger costs from Tesla API into TeslaMate" \
      org.opencontainers.image.source="https://github.com/YOUR_USERNAME/teslamate-supercharger-costs" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Install dependencies in a separate layer for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY importer.py .

# Persistent volumes for token cache and logs
VOLUME ["/data", "/logs"]

CMD ["python", "importer.py"]
