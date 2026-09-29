FROM python:3.11-slim
WORKDIR /app

# FFmpeg composes and normalizes videos; fonts are a fallback for image rendering.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN useradd --create-home --uid 1000 app
USER app

ENV PYTHONUNBUFFERED=1
# exec: uvicorn becomes PID 1 and receives SIGTERM for graceful shutdowns on redeploy.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8001}"]
