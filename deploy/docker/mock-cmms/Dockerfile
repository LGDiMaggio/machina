FROM python:3.11-slim-bookworm

RUN pip install --no-cache-dir fastapi uvicorn

COPY app.py /app/app.py
WORKDIR /app

EXPOSE 9000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
