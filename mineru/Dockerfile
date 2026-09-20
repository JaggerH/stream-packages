# Thin MinerU serving image (CPU by default; uses CUDA at runtime when present).
FROM python:3.11-slim

# libgl1 + libglib2.0-0 = opencv runtime (MinerU's layout/table models); git = deps fetches.
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# pipeline backend = deterministic-first (text-layer + dedicated table/formula models);
# modelscope = CN-domestic weights host (HuggingFace is GFW-blocked). Models download on
# first /parse to /root/.cache (persisted by the compose volume), like the whisperx image.
ENV MINERU_DEVICE=auto \
    MINERU_BACKEND=pipeline \
    MINERU_MODEL_SOURCE=modelscope
EXPOSE 80
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "80"]
