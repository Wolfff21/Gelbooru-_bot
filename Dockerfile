FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

EXPOSE 10000

CMD gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 bot:app
