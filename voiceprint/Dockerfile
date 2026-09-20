# GPU-ONLY sherpa-onnx image (user decision 2026-07-23: no CPU/GPU variants — CPU diarization
# runs ~0.9x realtime, a 2h episode takes ~2h, which defeats the feature; this deploy has a GPU).
# Consequence: package.json's stream.backend sets gpu: true, so `docker compose up` REQUIRES the nvidia container
# toolkit — a CPU-only host cannot start this service at all.
#
# GPU without a heavy nvidia/cuda base (same pattern as containers/whisper-asr): CUDA runtime
# libs come from pip wheels, libcuda.so itself comes from the host driver via `--gpus` (WSL2:
# bind-mounted from /usr/lib/wsl/lib). Avoids the ~670MB monolithic CUDA base-image layer that
# kept truncating through the proxy.
#
# The sherpa-onnx +cuda wheel (k2-fsa's own index) links CUDA **11** — verified by scanning the
# wheel's ELF NEEDED entries: libcudart.so.11.0 / libcublas.so.11 / libcudnn.so.8 / libcufft.so.10
# / libcurand.so.10 (libnvinfer.so.8 is the TensorRT provider — dlopen'd only if requested, we
# don't ship it). Hence the cu11 wheel family below, NOT cu12. Driver-side that's fine: any
# recent driver runs CUDA 11 apps.
FROM python:3.11-slim

# bzip2: the segmentation model ships as .tar.bz2; slim images lack the bzip2 codec `tar xf` needs.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl bzip2 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Two indexes on purpose: Tsinghua mirror for the big nvidia cu11 wheels (direct route through
# Clash, reliable for ~800MB of CUDA libs), k2-fsa find-links for the +cuda sherpa-onnx build
# (not on PyPI). Generous retries as belt-and-suspenders.
RUN pip install --no-cache-dir --retries 10 --timeout 120 \
      -i https://pypi.tuna.tsinghua.edu.cn/simple \
      -f https://k2-fsa.github.io/sherpa/onnx/cuda.html \
      -r requirements.txt

# Let onnxruntime's dlopen find the pip-installed CUDA 11 libs at runtime.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cuda_runtime/lib:/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.11/site-packages/nvidia/cufft/lib:/usr/local/lib/python3.11/site-packages/nvidia/curand/lib

# Bake the chosen sherpa-onnx model bundle pair. MODEL_VERSION (app.py) is the vector-comparability
# key for the registry — bump it if either asset below is ever swapped. Execution provider (cuda)
# does NOT affect vector comparability — same models, same weights, same 192-dim space.
#
# Segmentation: sherpa-onnx-pyannote-segmentation-3-0 (release "speaker-segmentation-models").
# Ships as a tar.bz2 bundle containing model.onnx plus metadata.
#
# Embedding: 3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx (release
# "speaker-recongition-models"), 192-dim, ERes2NetV2 trained on the zh-cn "common" large-scale
# corpus (3D-Speaker's current flagship Chinese SV model — best Chinese speaker discrimination) —
# ships as a standalone .onnx asset (no archive). 192-dim → the registry stores 192-float vectors.
#
# Both asset URLs verified via the GitHub releases API (assets exist). If a build ever fails on a
# curl step, check the current asset list at:
#   https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-segmentation-models
#   https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models
RUN mkdir -p /app/models && \
    curl -fL -o /tmp/seg.tar.bz2 \
      https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2 && \
    tar xf /tmp/seg.tar.bz2 -C /app/models && \
    rm /tmp/seg.tar.bz2 && \
    curl -fL -o /app/models/3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx \
      https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx

COPY app.py .

EXPOSE 80
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "80"]
