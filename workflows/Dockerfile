FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -q -r requirements.txt

# The whole package, so the image runs on its own (prod has no source
# mounts). .dockerignore keeps tests and caches out.
COPY . .
RUN chmod +x entrypoint.sh

EXPOSE 5678

# Supervised uvicorn: polls /healthz and exits non-zero on sustained failure
# so the restart policy recovers a wedged process. --reload only when
# UVICORN_RELOAD=1 (dev compose). See entrypoint.sh.
ENTRYPOINT ["/app/entrypoint.sh"]
