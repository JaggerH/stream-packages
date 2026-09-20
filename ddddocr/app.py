"""OCR for the `call` recipe step: one image in, one string out.

Deliberately the smallest possible surface. The contract this must satisfy is
`docs/PACKAGE.md` §4.2 for capability backends: **sub-second, stateless, replayable** — no long
compute, no stored results, the same input can be re-sent safely. OCR is naturally all three.

Why there is no `fetch-model.sh` here (every other self-built container has one): ddddocr ships
its ONNX weights *inside the pip wheel*. There is nothing to bake separately, and nothing is
downloaded at runtime — which is the property that rule was protecting in the first place.
"""
import base64
import binascii
import os
import threading

import ddddocr
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

# Instantiated once at import: the model is a few MB and loading it per request would turn a
# ~20ms call into a ~300ms one. `show_ad=False` silences the library's console banner.
#
# `beta=False` is the default ("old" model). It is the right one for the 4-character
# alphanumeric captchas this exists for; the beta model is tuned for a different distribution.
# If a site ever needs the other one, that is a NEW env-gated instance, not a runtime flag —
# switching models silently changes what the same image reads as.
_ocr = ddddocr.DdddOcr(show_ad=False, beta=False)

# `set_ranges()` **改的是那个共享实例的状态**，而它和紧随其后的 classification() 之间没有任何
# 原子性。两个并发请求各带一个字符集，就会出现"A 设了数字集、B 设了默认集、A 才开始识别"——
# A 拿回一个按 B 的字符集解出来的结果。表现是偶发的一次识别错，没有任何日志会提到它，而调用方
# 只会看到"识别不准"。锁住这一对，代价是把并发退化成串行；本服务按契约就是 ~20ms 的调用
# （`docs/PACKAGE.md` §4.2 的"秒级"），串行完全够用，用正确性换并发在这里是划算的。
_ocr_lock = threading.Lock()

# 图片上限。这不是性能考虑，是**别把一个错误的用法悄悄跑通**：call 那一格送的是
# "某个元素那一块的截图"（一个验证码框，几 KB），几 MB 的东西送到这儿来说明选择器选错了
# （多半选到了整页），而那正是 call 刻意不给的东西。宁可 413 说清楚。
MAX_IMAGE_BYTES = int(os.environ.get("DDDDOCR_MAX_IMAGE_BYTES", 512 * 1024))


class OcrRequest(BaseModel):
    """`image` 是 base64（`call` 步骤从 `driver.shotOf()` 拿到的就是这个形状）。

    允许带 `data:image/png;base64,` 前缀：浏览器侧两种写法都常见，在这儿多认一种比让调用方
    去猜便宜得多。
    """

    image: str
    # 只在这几个字符里挑答案（如 "0123456789"）。缺省 = 不限，用模型自己的全字符集。
    #
    # **为什么值得有**：默认模型是给字母数字混排调的，喂给它一张纯数字的图，它会把 0 认成 o、
    # 9 认成 g、2 认成 Z —— 东方财富的登录验证码实测 12 张错 3 张，错的全是这一类
    # （`74Z1` / `4g94` / `3o79`）。限住字符集之后那三张里的字母根本不在候选里。
    #
    # **为什么由调用方给、而不是写死在这儿**：限字符集是通用机制，限成哪一套是站点知识 ——
    # 写死就等于这个服务只服务一个站。recipe 在 `call.options` 里声明（那一格只收字面量）。
    charset: str | None = None


@app.get("/health")
def health():
    # 报**实际**能不能干活，不是"进程活着"。模型在 import 期就加载了，所以这里为真即为真；
    # 若将来改成惰性加载，这一格必须跟着改成真的探一次——一个恒 true 的 /health 比没有更坏。
    return {"ok": True, "model": "ddddocr", "beta": False}


def _classify(blob: bytes, charset: str | None) -> str:
    """识别一张图。给了 `charset` 就只在那几个字符里挑答案。

    两条路是**故意不一样**的，不是重复代码：

    - 不限字符集：直接 `classification(blob)`。它内部用模型的全字符集解码，**读都不读**
      `set_ranges` 设过的东西，所以上一个请求限过的字符集串不到这一个头上。
    - 限字符集：`set_ranges()` 之后必须走 `probability=True` —— 限制**只在概率那条分支里
      生效**（库里 `classification` 的实现如此）。拿回来的是每个时间步在受限字符集上的概率，
      逐位取最大的那个。不在模型原生字符集里的字符概率是 -1，永远选不中。

    **概率那条分支把解码扔给了调用方，而解码不是"逐位拼起来"。** 模型输出的是 CTC 序列：
    时间步比字符多，同一个字符会连着占好几步，字符之间用一个空白位隔开。库自己那条非概率
    路径解得很清楚——`if item == last_item: continue` 折叠连续重复，`if item != 0` 丢掉空白
    （`set_ranges` 会把那个空白以空串的形式放进候选里）。照着抄，一步都不能省：漏掉折叠的
    代价实测过，`6921` 会解成 **`69221`**（5 位），然后被 recipe 的 `^\\d{4}$` 判成不合格、
    白白重来一次——**锁字符集本来是来提高命中率的，漏了折叠反而会自己制造废图**。

    整段在锁里：`set_ranges` 改的是共享实例的状态，和随后的识别之间没有原子性（见 `_ocr_lock`）。
    """
    with _ocr_lock:
        if not charset:
            return _ocr.classification(blob) or ""
        _ocr.set_ranges(charset)
        res = _ocr.classification(blob, probability=True)
    charsets = res["charsets"]
    out: list[str] = []
    last: str | None = None
    for row in res["probability"]:
        ch = charsets[row.index(max(row))]
        if ch == last:
            continue
        last = ch
        if ch != "":
            out.append(ch)
    return "".join(out)


@app.post("/ocr")
def ocr(req: OcrRequest):
    raw = req.image
    if "," in raw[:64] and raw.lstrip().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="image 不是合法的 base64")
    if not blob:
        raise HTTPException(status_code=400, detail="image 解出来是空的")
    if len(blob) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"图片 {len(blob)} 字节，超过上限 {MAX_IMAGE_BYTES}。"
                "call 只该送一个元素那一块的截图——这么大多半是选择器选到了整页"
            ),
        )
    try:
        text = _classify(blob, req.charset)
    except Exception as exc:  # noqa: BLE001 — 底层库对坏图抛什么都有，一律当成 400
        raise HTTPException(status_code=400, detail=f"识别失败：{exc}")
    # 认不出就如实回空串，**不编一个**。调用方（call 的 `from` 取不到值就停）会因此中止，
    # 那正是想要的：填一个瞎猜的验证码进去，站点只会说"验证码错"，看起来像识别不准。
    return {"text": text or ""}
