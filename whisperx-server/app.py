"""Thin WhisperX HTTP wrapper for Stream's transcription plugin.

POST /transcribe  (multipart `file` = audio OR video; whisperx/ffmpeg extracts the
                   audio track) -> {text, language, segments[]}
GET  /health      -> {status, device, model}

CPU by default (compute_type int8); uses CUDA automatically when available.
Model size via WHISPERX_MODEL (default "small"). The model loads lazily once.
"""
import os
import tempfile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

app = FastAPI(title="whisperx-server")


def _device() -> str:
    pref = os.getenv("WHISPERX_DEVICE", "auto")
    if pref in ("cpu", "cuda"):
        return pref
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


DEVICE = _device()
COMPUTE = os.getenv("WHISPERX_COMPUTE") or ("float16" if DEVICE == "cuda" else "int8")
MODEL_NAME = os.getenv("WHISPERX_MODEL", "small")
BATCH = int(os.getenv("WHISPERX_BATCH", "8"))
# Whisper emits punctuation-free text for Chinese unless primed with a punctuated
# initial_prompt — the model is capable, the decoder just needs the cue. Env-tunable so the
# prompt can change without rebuilding. Set empty to disable.
PROMPT = os.getenv("WHISPERX_PROMPT", "以下是一段普通话内容的转写，包含逗号、句号、问号等标点符号。")
# Diarization (who-spoke-when) needs a HuggingFace token + accepting the pyannote license.
# whisperx 3.8.6's pyannote.audio loads the community-1 pipeline (even when asked for 3.1 it
# pulls community-1 assets), so that's the gated repo the user must accept. Env-overridable.
HF_TOKEN = os.getenv("HF_TOKEN")
DIARIZE_MODEL = os.getenv("WHISPERX_DIARIZE_MODEL", "pyannote/speaker-diarization-community-1")

_model = None
_diarize_pipe = None


def get_model():
    global _model
    if _model is None:
        import whisperx

        asr_options = {"initial_prompt": PROMPT} if PROMPT else None
        _model = whisperx.load_model(MODEL_NAME, DEVICE, compute_type=COMPUTE, asr_options=asr_options)
    return _model


def get_diarize():
    global _diarize_pipe
    if _diarize_pipe is None:
        import torch
        from whisperx.diarize import DiarizationPipeline

        _diarize_pipe = DiarizationPipeline(model_name=DIARIZE_MODEL, token=HF_TOKEN, device=torch.device(DEVICE))
    return _diarize_pipe


@app.get("/health")
def health():
    return {"status": "ok", "device": DEVICE, "model": MODEL_NAME, "diarization": bool(HF_TOKEN)}


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str | None = Form(None),
    diarize: bool = Form(False),
):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if diarize and not HF_TOKEN:
        raise HTTPException(status_code=503, detail="diarization unavailable: HF_TOKEN not set")
    suffix = os.path.splitext(file.filename or "")[1] or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        path = f.name
    try:
        import whisperx

        audio = whisperx.load_audio(path)  # ffmpeg: pulls the audio track from audio OR video
        result = get_model().transcribe(audio, batch_size=BATCH, language=language)
        lang = result.get("language")
        segs = result.get("segments", []) or []

        # Speaker diarization (opt-in): align for word timing → pyannote speaker turns →
        # tag each segment with its speaker. Heavier (two more models), hence per-request.
        if diarize:
            # Skip whisperx.align (it needs NLTK punkt_tab, fetched from GFW-blocked github).
            # assign_word_speakers does segment-level overlap against the diarization turns, so
            # word-level alignment isn't required to label each segment with a speaker.
            diar = get_diarize()(audio)
            result = whisperx.assign_word_speakers(diar, result)
            segs = result.get("segments", []) or []

        def seg(s):
            out = {"start": s.get("start"), "end": s.get("end"), "text": (s.get("text") or "").strip()}
            if s.get("speaker"):
                out["speaker"] = s["speaker"]
            return out

        return {
            "text": "\n".join((s.get("text") or "").strip() for s in segs).strip(),
            "language": lang,
            "segments": [seg(s) for s in segs],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"transcribe failed: {e}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
