# YTTOMP4 backend — Python + FastAPI + yt-dlp + ffmpeg
FROM python:3.12-slim

# ffmpeg is required by yt-dlp to merge video+audio into MP4.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p downloads
EXPOSE 8000

# Uvicorn serves the API. .env values are passed via docker-compose / -e.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
