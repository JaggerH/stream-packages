**仅存档，未接入 Stream。** 本地 ASR 兜底已退役（Stream 的 STT 走云端梯子），源码留着以备重建。

# whisperx-server

Thin WhisperX HTTP wrapper（`POST /transcribe` multipart `file` 音频或视频 → `{text, language, segments[]}`，
`GET /health`）。CPU 默认 int8，有 CUDA 自动用；模型大小由 `WHISPERX_MODEL`（默认 `small`）指定，首次
请求时懒加载。`whisper-asr/` 是它的后继（faster-whisper，拆掉了 diarization）。
