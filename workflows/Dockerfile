FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -q -r requirements.txt

COPY app.py telegram.py llm_proxy.py models.json .

EXPOSE 5678

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5678", "--reload"]
