import io
import os
import tempfile
import subprocess
from fastapi import FastAPI, UploadFile
from faster_whisper import WhisperModel

# Model + precision are env-driven so the SAME image can be run at different VRAM/quality tiers:
#   WHISPER_MODEL:        large-v3 | medium | small | ...
#   WHISPER_COMPUTE_TYPE: float16 | int8_float16 | int8   (CTranslate2 compute types)
#   WHISPER_DEVICE:       cuda | cpu
MODEL = os.environ.get("WHISPER_MODEL", "large-v3")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
DEVICE = os.environ.get("WHISPER_DEVICE", "cuda")

app = FastAPI()
_model = None


def _load():
    global _model
    if _model is None:
        # MODEL is a baked-in local dir by default (/models/large-v3, see Dockerfile) — no
        # download at load. A HF model name still works if a user opts into runtime fetch.
        _model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE_TYPE)
    return _model


def _read_audio(raw: bytes) -> str:
    # faster-whisper accepts a file path or a numpy array; decode arbitrary media to 16k mono wav
    # via ffmpeg first so mp4/m4a/etc. all work, then hand the temp path to the model.
    f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    src = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
    src.write(raw)
    src.flush()
    subprocess.run(
        ["ffmpeg", "-y", "-i", src.name, "-ac", "1", "-ar", "16000", f.name, "-loglevel", "error"],
        check=True,
    )
    return f.name


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL, "compute_type": COMPUTE_TYPE, "device": DEVICE}


@app.post("/transcribe")
async def transcribe(file: UploadFile):
    global _model
    model = _load()
    path = _read_audio(await file.read())
    try:
        # ASR ONLY — plain speech→text with VAD-based segments. No diarization here by design.
        # beam_size=1 (vs the default 5) keeps decoder VRAM low — on a shared consumer GPU,
        # large-v3 + a 5-wide beam can OOM even while nvidia-smi shows memory free (WSL2 GPU
        # paravirtualization + desktop contention). The segments generator is lazy, so the
        # actual inference (and any OOM) happens while iterating it below — keep it in the try.
        segments, info = model.transcribe(path, vad_filter=True, beam_size=1)
        segs = [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()} for s in segments]
    except RuntimeError as e:
        # A CUDA OOM poisons the process's CUDA context — every later request then fails with
        # "invalid device ordinal" until the container restarts, taking the local ASR fallback
        # dark. Reset the model singleton so the NEXT request rebuilds a fresh context and
        # self-heals, instead of one transient OOM bricking the backend.
        if "CUDA" in str(e) or "out of memory" in str(e):
            _model = None
        raise
    text = "\n".join(s["text"] for s in segs)
    return {"model": MODEL, "compute_type": COMPUTE_TYPE, "language": info.language, "segments": segs, "text": text}
