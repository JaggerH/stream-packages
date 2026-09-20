**仅存档，未接入 Stream。** 本地 ASR 兜底已退役（Stream 的 STT 走云端梯子），源码留着以备重建。

# whisper-asr

ASR-only container: faster-whisper (CTranslate2) speech→text，不含 diarization（那是 `voiceprint/`）。
`Dockerfile` + `app.py` + `fetch-model.sh`（模型拉到 `models/`，被 gitignore）。GPU 走 pip 轮子的
cuDNN / cuBLAS，`libcuda.so` 来自宿主驱动；CPU 主机设 `WHISPER_DEVICE=cpu`。
