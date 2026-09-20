"""Thin MinerU HTTP wrapper for Stream's document-parsing plugin.

POST /parse   (multipart `file` = image OR pdf; `type` = "pdf"|"image" hint)
              -> {markdown, json}
GET  /health  -> {status, device, backend}

Deterministic-first by design: MinerU's `pipeline` backend extracts a born-digital
PDF's text layer directly (exact text/numbers, no fabrication) and runs dedicated
table/formula models; the VLM only handles scanned/image regions. We deliberately do
NOT expose an end-to-end "re-read everything with a VLM" mode — that is the unfaithful
path this whole feature exists to avoid.

CPU by default; uses CUDA automatically when present. Models download on first use to
/root/.cache (persisted by the compose volume), like the whisperx image.
"""
import glob
import os
import subprocess
import tempfile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

app = FastAPI(title="mineru-server")

# pipeline (default) = the deterministic-first multi-stage pipeline (layout → table → formula
# → reading order). Keep it; the VLM backends trade faithfulness for convenience.
BACKEND = os.getenv("MINERU_BACKEND", "pipeline")
# CN-domestic model host by default (HuggingFace is GFW-blocked); override to "huggingface".
MODEL_SOURCE = os.getenv("MINERU_MODEL_SOURCE", "modelscope")
PARSE_TIMEOUT = int(os.getenv("MINERU_TIMEOUT", "600"))

_EXT = {"pdf": ".pdf", "image": ".png"}


def _device() -> str:
    pref = os.getenv("MINERU_DEVICE", "auto")
    if pref in ("cpu", "cuda"):
        return pref
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


DEVICE = _device()


@app.get("/health")
def health():
    return {"status": "ok", "device": DEVICE, "backend": BACKEND}


@app.post("/parse")
async def parse(file: UploadFile = File(...), type: str = Form("pdf")):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    # suffix from the upload name, else from the type hint (mineru routes by extension)
    suffix = os.path.splitext(file.filename or "")[1] or _EXT.get(type, ".pdf")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, f"doc{suffix}")
        out = os.path.join(tmp, "out")
        with open(src, "wb") as f:
            f.write(data)
        try:
            proc = subprocess.run(
                ["mineru", "-p", src, "-o", out, "-b", BACKEND, "-d", DEVICE, "--source", MODEL_SOURCE],
                capture_output=True,
                text=True,
                timeout=PARSE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="parse timed out")
        if proc.returncode != 0:
            raise HTTPException(status_code=502, detail=f"mineru failed: {proc.stderr[-800:]}")

        # pipeline writes <out>/<stem>/auto/<stem>.md (+ *_content_list.json). Glob defensively
        # so a backend/version layout change still finds the markdown (largest .md wins).
        mds = sorted(glob.glob(os.path.join(out, "**", "*.md"), recursive=True), key=os.path.getsize, reverse=True)
        if not mds:
            raise HTTPException(status_code=502, detail="mineru produced no markdown")
        with open(mds[0], encoding="utf-8") as f:
            markdown = f.read()

        blocks = None
        cls = glob.glob(os.path.join(out, "**", "*_content_list.json"), recursive=True)
        if cls:
            try:
                import json

                with open(cls[0], encoding="utf-8") as f:
                    blocks = json.load(f)
            except Exception:
                blocks = None

        return {"markdown": markdown, "json": blocks}
