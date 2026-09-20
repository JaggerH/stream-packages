"""去水印：找角落里的平台水印标，用 LaMa 补掉。

契约（能力型后端，PACKAGE.md §4.2）：
    POST /remove   multipart `image`（PNG/JPEG）→ image/png
                   可选 query `box=x,y,w,h`（像素）：跳过检测、直接补这一块。
    GET  /health   → {"ok": true}

两步：
1. 找：对四个角各裁一块，与 `templates/` 里的标做多尺度 `matchTemplate`（TM_CCOEFF_NORMED）。
   最高分 ≥ MATCH_MIN 就用它；一个都不到 → **回退左上固定框**，并在响应头 `x-dewatermark: fallback`
   里说出来（静默回退 = 下一个人以为检测一直在工作）。
2. 补：LaMa（iopaint）。mask 比框各外扩 PAD 像素——标的边缘是半透明羽化的，贴着框补会留一圈淡边。
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, Response, UploadFile
from iopaint.model_manager import ModelManager
from iopaint.schema import InpaintRequest
from PIL import Image

MATCH_MIN = float(os.environ.get("DEWATERMARK_MATCH_MIN", "0.6"))
# 角落搜索窗：短边的这个比例。豆包预览图的标约占短边 7%，留一倍余量。
CORNER_FRAC = 0.2
SCALES = [0.6, 0.75, 0.9, 1.0, 1.15, 1.3, 1.5]
PAD = 6
# 回退框（相对尺寸，左上角）：2048² 样张上标占 x 39–298 / y 39–149，即 ~14.6% × 7.3%，各留一点。
FALLBACK = (0.015, 0.015, 0.145, 0.065)

app = FastAPI()
_model: ModelManager | None = None
_templates: list[np.ndarray] = []


def model() -> ModelManager:
    global _model
    if _model is None:
        _model = ModelManager(name="lama", device="cpu")
    return _model


def templates() -> list[np.ndarray]:
    if not _templates:
        for p in sorted(Path(__file__).parent.joinpath("templates").glob("*.png")):
            t = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if t is not None:
                _templates.append(t)
    return _templates


def corners(h: int, w: int) -> tuple[list[tuple[int, int]], int]:
    """四个角的搜索窗左上角 (y, x) 与窗边长。"""
    s = int(min(h, w) * CORNER_FRAC)
    return [(0, 0), (0, w - s), (h - s, 0), (h - s, w - s)], s


def find_box(gray: np.ndarray) -> tuple[tuple[int, int, int, int], float] | None:
    """在四个角里找模板；回 (x, y, w, h) 与得分，找不到回 None。"""
    h, w = gray.shape
    (spots, s) = corners(h, w)
    best: tuple[float, tuple[int, int, int, int]] | None = None
    for t in templates():
        for scale in SCALES:
            tw, th = int(t.shape[1] * scale), int(t.shape[0] * scale)
            if tw < 8 or th < 8 or tw >= s or th >= s:
                continue
            ts = cv2.resize(t, (tw, th), interpolation=cv2.INTER_AREA)
            for (cy, cx) in spots:
                win = gray[cy : cy + s, cx : cx + s]
                res = cv2.matchTemplate(win, ts, cv2.TM_CCOEFF_NORMED)
                _, score, _, loc = cv2.minMaxLoc(res)
                if best is None or score > best[0]:
                    best = (float(score), (cx + loc[0], cy + loc[1], tw, th))
    if best is None or best[0] < MATCH_MIN:
        return None
    return best[1], best[0]


def inpaint(rgb: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    h, w = rgb.shape[:2]
    x, y, bw, bh = box
    x0, y0 = max(0, x - PAD), max(0, y - PAD)
    x1, y1 = min(w, x + bw + PAD), min(h, y + bh + PAD)
    mask = np.zeros((h, w), dtype=np.uint8)
    mask[y0:y1, x0:x1] = 255
    # iopaint 吃 RGB、回 BGR（沿 lama-cleaner 的老约定）
    out_bgr = model()(rgb, mask, InpaintRequest())
    return cv2.cvtColor(out_bgr.astype(np.uint8), cv2.COLOR_BGR2RGB)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "templates": len(templates())}


@app.post("/remove")
async def remove(image: UploadFile = File(...), box: str | None = Query(default=None)) -> Response:
    raw = await image.read()
    try:
        pil = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"not an image: {e}") from e
    # np.array 不是 np.asarray：后者给的是只读视图，iopaint 会往结果数组里原地写。
    rgb = np.array(pil)
    h, w = rgb.shape[:2]
    headers: dict[str, str] = {}
    if box:
        try:
            x, y, bw, bh = (int(v) for v in box.split(","))
        except ValueError as e:
            raise HTTPException(400, "box must be x,y,w,h in pixels") from e
        target = (x, y, bw, bh)
        headers["x-dewatermark"] = "explicit"
    else:
        found = find_box(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))
        if found:
            target, score = found
            headers["x-dewatermark"] = f"matched;score={score:.2f}"
        else:
            fx, fy, fw, fh = FALLBACK
            target = (int(w * fx), int(h * fy), int(w * fw), int(h * fh))
            headers["x-dewatermark"] = "fallback"
    headers["x-dewatermark-box"] = ",".join(str(v) for v in target)
    out = inpaint(rgb, target)
    buf = io.BytesIO()
    Image.fromarray(out).save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png", headers=headers)
