FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -q -r requirements.txt

COPY app.py telegram.py llm_proxy.py models.json entrypoint.sh ./
RUN chmod +x entrypoint.sh

EXPOSE 5678

# Supervised uvicorn: polls /healthz and exits non-zero on sustained failure
# so the restart policy recovers a wedged --reload reloader. See entrypoint.sh.
ENTRYPOINT ["/app/entrypoint.sh"]
