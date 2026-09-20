# Thin WhisperX serving image (CPU by default; uses CUDA at runtime when present).
FROM python:3.11-slim

# ffmpeg = audio extraction (whisperx.load_audio); git = some model/deps fetches.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV WHISPERX_MODEL=small \
    WHISPERX_DEVICE=auto
EXPOSE 80
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "80"]
