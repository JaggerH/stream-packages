"""容器冒烟：拿几张真实样张打 /remove，看检测是否命中、角落是否真的被改了。

用法：python containers/dewatermark/smoke.py http://127.0.0.1:8900/_p/dewatermark 样张1.png [样张2.png …]
判据：每张响应头 `x-dewatermark` 以 `matched` 开头（不是 fallback），且框内像素与原图的均差 > 8。
"""
import io
import sys
import urllib.request

import numpy as np
from PIL import Image


def post(base: str, path: str) -> tuple[bytes, dict]:
    boundary = "----stream-smoke"
    raw = open(path, "rb").read()
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"x.png\"\r\n"
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + raw + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(base.rstrip("/") + "/remove", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=120) as res:
        return res.read(), dict(res.headers)


def main() -> int:
    base, paths = sys.argv[1], sys.argv[2:]
    bad = 0
    for p in paths:
        out, headers = post(base, p)
        tag = headers.get("x-dewatermark", "")
        box = [int(v) for v in headers.get("x-dewatermark-box", "0,0,0,0").split(",")]
        a = np.asarray(Image.open(p).convert("RGB")).astype(int)
        b = np.asarray(Image.open(io.BytesIO(out)).convert("RGB")).astype(int)
        x, y, w, h = box
        diff = np.abs(a[y : y + h, x : x + w] - b[y : y + h, x : x + w]).mean()
        ok = tag.startswith("matched") and diff > 8
        bad += 0 if ok else 1
        print(f"{'OK ' if ok else 'BAD'} {p}: {tag} box={box} diff={diff:.1f}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
