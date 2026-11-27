FROM python:3.12-slim

LABEL org.opencontainers.image.title="teslamate-supercharger-costs" \
      org.opencontainers.image.description="Imports real Supercharger costs from the Tesla Fleet API into TeslaMate" \
      org.opencontainers.image.source="https://github.com/kalich5/teslamate-supercharger-costs" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Install dependencies in a separate layer for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY templates/ templates/
COPY static/ static/
COPY translations/ translations/

# Persistent volumes for token, settings, keys and logs
VOLUME ["/data", "/logs"]

ENV DATA_DIR=/data \
    ROOT_PATH=/supercharger \
    PORT=4002
EXPOSE 4002

HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}{os.environ[\"ROOT_PATH\"].rstrip(\"/\")}/health')"

# Web UI + nightly scheduler.  One-off CLI run:
#   docker compose run --rm importer python importer.py --dry-run
CMD ["python", "web.py"]
