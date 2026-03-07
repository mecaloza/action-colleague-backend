FROM python:3.11-slim
WORKDIR /app

# Install fonts for Pillow infographic generation + FFmpeg for video merging
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    fonts-liberation \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8001}
