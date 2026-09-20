# ASR-ONLY container — faster-whisper (CTranslate2) for speech→text. Deliberately contains NO
# diarization: who-spoke-when + speaker embeddings live in the separate `voiceprint` container
# (sherpa-onnx). Keeping ASR and diarization in separate images keeps the two axes decoupled —
# ASR can be CF (cloud) or this local container; diarization is always the voiceprint container.
#
# GPU without a heavy nvidia/cuda base: cuDNN 9 + cuBLAS for CUDA 12 come from pip wheels
# (see requirements.txt) and libcuda.so comes from the host driver via `--gpus` (on WSL2 the
# driver libs are bind-mounted from /usr/lib/wsl/lib). This avoids the ~670MB monolithic CUDA
# base-image layer, which was repeatedly truncated pulling through the proxy. On a CPU-only host
# set WHISPER_DEVICE=cpu.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Domestic pip mirror (Tsinghua): the international pypi path through the proxy keeps truncating
# large-wheel SSL connections; the mirror routes direct (Clash sends CN domains direct) and is
# reliable for the ~500MB cuDNN wheel. Generous retries as a belt-and-suspenders.
RUN pip3 install --no-cache-dir --retries 10 --timeout 120 \
      -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# Let CTranslate2's dlopen find the pip-installed CUDA libs at runtime.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib

# Bake the default model (fetched by fetch-model.sh into ./models/large-v3 before build) so the
# container is self-contained — no runtime HF pull (which fails through the proxy). Override the
# tier via WHISPER_MODEL/WHISPER_COMPUTE_TYPE env if a bind-mounted model is preferred.
COPY models/large-v3 /models/large-v3
ENV WHISPER_MODEL=/models/large-v3

# wget for the compose healthcheck (`wget -qO- http://localhost:80/health`) — the slim base and
# our ffmpeg apt line don't ship it, so without this the container is stuck "unhealthy". Kept as a
# late layer so it doesn't bust the cached cuDNN-wheel + 3GB-model layers above.
RUN apt-get update && apt-get install -y --no-install-recommends wget && rm -rf /var/lib/apt/lists/*

COPY app.py .
EXPOSE 80
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "80"]
