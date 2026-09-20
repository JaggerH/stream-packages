import io
import itertools
import json
import math
import os
import shutil
import tarfile
import tempfile
import threading
import time
import subprocess
from concurrent import futures
import urllib.request
import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, UploadFile, Form
import sherpa_onnx

# ---------------------------------------------------------------------------------------------
# Contract: seconds-scale, stateless, replayable. This container diarizes exactly ONE window per
# request — it does not know about, and does not window, long recordings. Slicing a long
# recording into windows and merging speaker identities across those windows is the CALLER's job
# (see src/voiceprint/windowed.ts and docs/superpowers/plans/2026-07-23-capability-job-runner.md);
# this container has no state across requests to do that merge itself.
#
# Why a per-request duration cap exists at all: feeding a whole long recording into sherpa-onnx's
# OfflineSpeakerDiarization grows memory with duration and killed the container in production — a
# 355s clip OOM'd a 4GiB mem_limit (~5m51s, exit 137, reproduced twice — see git history /
# docs/TODO.md removal commit). /diarize now rejects (413) any single request whose audio exceeds
# VOICEPRINT_MAX_SINGLE_S rather than trying to survive it — the caller is expected to have
# already windowed the source.
#
# EMBED_CLIP_MAX_S remains a separate, unconditional cap: per-segment embeddings are computed on
# at most EMBED_CLIP_MAX_S seconds of that segment's audio. This is NOT about the windowing that
# moved to the caller — it is the fix for the embedding-side OOM (long segments' embeddings
# saturate anyway; unbounded clips blow up ONNX activation memory), and it applies to every
# segment on every request, including a single-speaker segment that runs the length of an entire
# (already-windowed, <=300s) request.
# ---------------------------------------------------------------------------------------------
MAX_SINGLE_S = float(os.environ.get("VOICEPRINT_MAX_SINGLE_S", "300"))
EMBED_CLIP_MAX_S = 30.0      # max audio used for any returned segment's embedding

# Vector-comparability key: the registry stores this per voiceprint, so any change to the
# baked segmentation/embedding models MUST be paired with a version bump here.
MODEL_VERSION = os.environ.get("VOICEPRINT_MODEL_VERSION", "sherpa-eres2netv2-zhcn-192-v1")

# ---------------------------------------------------------------------------
# 二次归组（regroup）——治「一个人连续讲满一窗，却被判成主说话人 + 一堆三五秒碎片」。
#
# 为什么第一遍会错，实测（喜剧之王 E02，两个纯度 100%/94% 的单人区段，块长扫描）：
#
#   块长    同人中位  同人p95   异人中位  异人p05    有没有干净刀口
#    1s     0.537    0.804    0.721    0.495    重叠 -0.309
#    2s     0.398    0.690    0.658    0.459    重叠 -0.231
#    4s     0.271    0.527    0.587    0.422    重叠 -0.105
#    8s     0.167    0.348    0.537    0.370    有间隙 +0.021
#   16s     0.109    0.197    0.512    0.351    有间隙 +0.155
#
# 「有干净刀口」= 同人里最差的 5% 仍比异人里最像的 5% 更近，只有这时才存在一个能用的阈值。
# **刀口要到 8 秒才出现，16 秒才舒服**。而第一遍聚类吃的是分段结果——该集段长中位 2.2s、
# 四分之一短于 1.1s，整个落在「不存在任何可用阈值」的区间里。这不是聚类调得不好，是被要求
# 用 2 秒的音频回答一个 8 秒才答得了的问题。
#
# 所以第二遍不碰算法参数，改喂给它的**音频量**：把同一个（第一遍判出的）说话人的若干段
# **拼起来**重算一个嵌入，再按同人/异人分布定出的阈值重聚一次。手法不是新发明——
# 这个文件算跨窗代表时本来就是 concat 重算，只是窗内聚类那一步没用上。
#
# 音频量不够 MIN_REP_S 的说话人不参与决定分组（它的嵌入本来就说不了话），聚完就近归附。
# 注意：这一步只改**标签**，不改嵌入空间 → MODEL_VERSION 不动，存量向量仍可比。
REGROUP = os.environ.get("VOICEPRINT_REGROUP", "1") != "0"
REP_CLIP_S = float(os.environ.get("VOICEPRINT_REP_CLIP_S", "20"))     # 每人最多拼多少秒重算代表
MIN_REP_S = float(os.environ.get("VOICEPRINT_MIN_REP_S", "8"))        # 低于它不参与分组决策
REGROUP_THRESHOLD = float(os.environ.get("VOICEPRINT_REGROUP_THRESHOLD", "0.3"))

# ---------------------------------------------------------------------------
# 帧级人声门控（frame gate）——「谁贡献指纹」这一步只收人声帧。
#
# 治的是这个：综艺现场的掌声/欢呼/罐头笑声与人声在**时间上就没被分开**。报幕一句 +
# 全场鼓掌被分段模型判成同一段，于是拿这段音频算出来的代表，一半的信息是现场声。
# 嵌入模型对「人声+掌声混合」整段算特征，污染是**非线性烧进每个数字**的——事后在向量
# 上做减法/投影救不回来（两轮实证已证伪，见 docs/research/voiceprint-clustering.md）。
# 唯一还站得住的地方是**在音频进嵌入模型之前把脏帧扔掉**。
#
# 为什么相信帧级分得开，而「声学特征分不开」那条旧结论不适用：旧结论量的是**段级统计**
# 上的手工特征（过零率/谱熵），真人上界 0.728 vs 非人下界 0.739 只差 0.011；而训练过的
# 事件分类器在 1s 帧粒度上，金标段人声主导帧占比 20–32%、纯人声对照 80–100%，**中间无
# 重叠**。是「特征 + 粒度」的双重局限，不是信息本身不存在。
#
# 用的是 sherpa-onnx 自带的 audio tagging（AudioSet 527 类，zipformer，int8）——不是 PANNs
# 本尊，但同一件事：AudioSet 527 类事件分类器。选它的理由是**零新依赖**：分类器跑在这个
# 镜像已经有的 sherpa-onnx 上，吃原始波形（frontend 在模型里，不必自己复刻 log-mel 参数——
# PANNs 的 ONNX 导出恰恰要求外部复刻 torchlibrosa 的 STFT，是个静默算错的坑）。
#
# 判据（三件套）：
#   1. 1s 帧粒度逐帧分类；
#   2. speech_p > GATE_SPEECH_MIN 且 speech_p > GATE_SPEECH_RATIO × nonspeech_p 才留；
#   3. 门控后剩余帧不足 8s **且大部分音频是被门控扔掉的** → 该说话人本窗**弃权**：
#      不出干净代表。两个条件缺一不可（只用 8s 会把「干净但短」的人一起误伤，
#      实测代价见 `_abstains()`）。
#
# **帧长为什么是 1s 而不是 0.5s**（实测定的，别照直觉往下调）：这类 AudioSet 分类器训练
# 时吃的是 10s 片段，0.5s 上它整体不自信——E02 金标自检里连贯人声对照段只有 30–42% 的帧
# 过得了 `speech_p > 0.5`（speech_p 中位数才 0.14–0.33），等于把六成真人声当脏帧扔掉。
# 换 1s 立刻回到对照 88–100% / 金标 5–28%（一个 69% 的离群段见 spec），与研究档里 PANNs
# Cnn14 @1s 的实测同一量级。帧短反而更糊，是因为**信息量不够**，不是阈值没调好——
# 把 0.5s 的阈值往下压只会同时放进笑声。顺带一提 1s 还更便宜（每帧摊到的固定开销更少）。
#
# 8s 这个下限不是拍脑袋：块长扫描实测（见上面 REGROUP 头注的表）同人/异人分布**到 8 秒
# 才首次出现刀口**。剩不到 8 秒干净人声，就是「拿 2 秒的音频回答 8 秒才答得了的问题」，
# 与其给一个说不了话的代表，不如明说自己答不了。
#
# 弃权的语义（下游契约，别想当然）：segments 与它们的段级 embedding **原样返回**，
# 时间线一秒不动——门控只决定「谁贡献指纹」，不决定「谁说过话」。弃权者在 speakers[]
# 里以 `abstained: true` + 空 embedding 显式出现（**不是**悄悄缺席：缺席在下游会被当成
# 「老容器」而退回段级均值，正好是要拦的那个脏东西）。
GATE = os.environ.get("VOICEPRINT_FRAME_GATE", "1") != "0"
GATE_FRAME_S = float(os.environ.get("VOICEPRINT_GATE_FRAME_S", "1.0"))
GATE_SPEECH_MIN = float(os.environ.get("VOICEPRINT_GATE_SPEECH_MIN", "0.5"))
GATE_SPEECH_RATIO = float(os.environ.get("VOICEPRINT_GATE_SPEECH_RATIO", "1.5"))
# 弃权判据的两个门槛，含义与实测依据见 `_abstains()`。
GATE_MIN_CLEAN_S = float(os.environ.get("VOICEPRINT_GATE_MIN_CLEAN_S", "8"))
GATE_ABSTAIN_RATIO = float(os.environ.get("VOICEPRINT_GATE_ABSTAIN_RATIO", "0.5"))
# 逐帧分类的并行度（见 `_gate_spans`）。1 = 顺序，与并行版逐位同结果，用来对照排错。
GATE_JOBS = max(1, int(os.environ.get("VOICEPRINT_GATE_JOBS", "4")))

# 模型不进 git、也不烤进镜像：启动时拉一次，落在 compose 挂的 voiceprint-cache 卷上
# (/root/.cache)，之后容器重建也不用重下。只解包 int8 权重与标签表（tar 里还有一份 259MB
# 的 fp32 权重和 test_wavs，用不上）。
AT_DIR = os.environ.get("VOICEPRINT_AT_DIR", "/root/.cache/audio-tagging")
AT_BUNDLE = "sherpa-onnx-zipformer-audio-tagging-2024-04-09"
AT_URL = os.environ.get(
    "VOICEPRINT_AT_URL",
    f"https://github.com/k2-fsa/sherpa-onnx/releases/download/audio-tagging-models/{AT_BUNDLE}.tar.bz2",
)
AT_MODEL = f"{AT_DIR}/{AT_BUNDLE}/model.int8.onnx"
AT_LABELS = f"{AT_DIR}/{AT_BUNDLE}/class_labels_indices.csv"

# AudioSet 类名（按 display_name 匹配，不写死类下标——下标是模型 bundle 的实现细节）。
# 人声：只收「有人在说话」的类。**唱歌不算**——要的是说话人身份，歌声既不是常态说话
# 音色、也常和伴奏绑在一起。
GATE_SPEECH_LABELS = {
    "Speech",
    "Male speech, man speaking",
    "Female speech, woman speaking",
    "Child speech, kid speaking",
    "Conversation",
    "Narration, monologue",
}
# 非人声：综艺现场实际污染源。注意 "Hubbub, speech noise, speech babble" 在 AudioSet
# 本体里挂在 Human voice 下，但它正是「全场嗡嗡」——按用途归非人声。
GATE_NONSPEECH_LABELS = {
    "Laughter",
    "Baby laughter",
    "Giggle",
    "Snicker",
    "Belly laugh",
    "Chuckle, chortle",
    "Applause",
    "Clapping",
    "Cheering",
    "Crowd",
    "Booing",
    "Hubbub, speech noise, speech babble",
    "Chatter",
    "Children playing",
    "Music",
    "Singing",
    "Choir",
    "Musical instrument",
    "Theme music",
    "Background music",
}

# Provider is CPU by default and CUDA only when explicitly opted in (VOICEPRINT_USE_CUDA=1).
# Why opt-in rather than "try cuda, fall back": the plain PyPI sherpa-onnx wheel this image ships
# has NO CUDA execution provider, and passing provider="cuda" to it does NOT raise — it silently
# runs on CPU. So a "try cuda first" default would make /health report "cuda" while actually on
# CPU (a lie). The GPU image variant (CUDA base + GPU-enabled sherpa-onnx wheel) sets
# VOICEPRINT_USE_CUDA=1 to request cuda; _load() still keeps a cuda->cpu try/except as a safety
# net. ACTIVE_PROVIDER (set by _load()) is what /health reports — the provider actually requested.
PROVIDER = "cuda" if os.environ.get("VOICEPRINT_USE_CUDA") == "1" else "cpu"
ACTIVE_PROVIDER = None
# Default hint value when the request omits one; wired from env so package.json's
# VOICEPRINT_HINT_DEFAULT isn't dead config (the hint itself is still a no-op — see /diarize).
HINT_DEFAULT = os.environ.get("VOICEPRINT_HINT_DEFAULT", "accuracy")

# Model paths baked into the image by the Dockerfile (see containers/voiceprint/Dockerfile).
SEG_MODEL = os.environ.get(
    "VOICEPRINT_SEG_MODEL",
    "/app/models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
)
EMB_MODEL = os.environ.get(
    "VOICEPRINT_EMB_MODEL",
    "/app/models/3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx",
)

app = FastAPI()

# Lazy singletons: a diarization pipeline (segmentation + clustering) and a standalone speaker
# embedding extractor. Note this is a DELIBERATE double load of the embedding model: one copy
# lives inside diar_config.embedding for the pipeline's internal clustering, and a second
# standalone _embed extractor is built separately, because sd.process() (the diarization
# pipeline) does not expose per-segment embeddings — a standalone extractor is required to
# compute them for /diarize segments and for the standalone /embed endpoint. Do not "simplify"
# this to a single instance; that would break embedding extraction.
_diar = None
_embed = None


def _build(provider):
    """Construct (diar, embed) for a given provider string. Raises if the provider backend
    (e.g. CUDA) isn't actually available in this sherpa-onnx build/host."""
    diar_config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=SEG_MODEL),
            provider=provider,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=EMB_MODEL, provider=provider),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.5),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    diar = sherpa_onnx.OfflineSpeakerDiarization(diar_config)

    embed_config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=EMB_MODEL, provider=provider)
    embed = sherpa_onnx.SpeakerEmbeddingExtractor(embed_config)
    return diar, embed


def _load():
    global _diar, _embed, ACTIVE_PROVIDER
    if PROVIDER == "cpu":
        _diar, _embed = _build("cpu")
        ACTIVE_PROVIDER = "cpu"
        return
    try:
        _diar, _embed = _build("cuda")
        ACTIVE_PROVIDER = "cuda"
    except Exception:
        # No CUDA execution provider available (CPU-only image/host) — fall back to CPU so the
        # service still starts and serves requests, just without GPU acceleration.
        _diar, _embed = _build("cpu")
        ACTIVE_PROVIDER = "cpu"


# --- 帧级人声门控：模型获取与加载 ------------------------------------------------
# 单例 + 一把锁。第一个要用它的请求负责加载；导入时另起一个后台线程预拉，好让常态下
# 第一次 /diarize 不用等下载。/health 不碰这条路径（compose 的 healthcheck 只给 5s，
# 让它去等一个 286MB 的下载会把容器判成 unhealthy 并反复重启）。
_at = None
_at_speech_idx: set = set()
_at_nonspeech_idx: set = set()
_at_lock = threading.Lock()
_at_error = None


def _fetch_tagging_model():
    """把 audio tagging 模型拉到缓存卷。已存在则直接返回。

    先下到临时目录再整体 rename——半个模型留在缓存里比没有模型更糟（下次启动会以为
    它在，然后在加载时炸）。
    """
    if os.path.exists(AT_MODEL) and os.path.exists(AT_LABELS):
        return
    os.makedirs(AT_DIR, exist_ok=True)
    staging = tempfile.mkdtemp(dir=AT_DIR, prefix=".staging-")
    try:
        arc = os.path.join(staging, "bundle.tar.bz2")
        urllib.request.urlretrieve(AT_URL, arc)
        want = {f"{AT_BUNDLE}/model.int8.onnx", f"{AT_BUNDLE}/class_labels_indices.csv"}
        with tarfile.open(arc, "r:bz2") as tf:
            members = [m for m in tf.getmembers() if m.name in want]
            if len(members) != len(want):
                raise RuntimeError(f"audio-tagging bundle missing members: {want - {m.name for m in members}}")
            tf.extractall(staging, members=members)
        final = f"{AT_DIR}/{AT_BUNDLE}"
        if not os.path.exists(final):
            os.rename(os.path.join(staging, AT_BUNDLE), final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _load_tagger():
    """返回 AudioTagging 单例；不可用时返回 None 并把原因记在 _at_error。"""
    global _at, _at_error, _at_speech_idx, _at_nonspeech_idx
    if _at is not None:
        return _at
    with _at_lock:
        if _at is not None:
            return _at
        try:
            _fetch_tagging_model()
            # 标签表把 display_name 映射回下标；按名字匹配，换 bundle 也不会静默错位。
            speech, nonspeech = set(), set()
            with open(AT_LABELS, "r", encoding="utf-8") as f:
                next(f, None)  # header: index,mid,display_name
                for line in f:
                    # display_name 自带逗号（"Male speech, man speaking"），只切前两刀
                    parts = line.rstrip("\n").split(",", 2)
                    if len(parts) < 3:
                        continue
                    idx, name = parts[0], parts[2].strip().strip('"')
                    if name in GATE_SPEECH_LABELS:
                        speech.add(int(idx))
                    elif name in GATE_NONSPEECH_LABELS:
                        nonspeech.add(int(idx))
            missing = len(GATE_SPEECH_LABELS) - len(speech)
            if not speech or not nonspeech:
                raise RuntimeError("audio-tagging labels file matched no speech/non-speech classes")
            cfg = sherpa_onnx.AudioTaggingConfig(
                model=sherpa_onnx.AudioTaggingModelConfig(
                    zipformer=sherpa_onnx.OfflineZipformerAudioTaggingModelConfig(model=AT_MODEL),
                    num_threads=int(os.environ.get("VOICEPRINT_AT_THREADS", "2")),
                    # 门控恒定跑 CPU：帧粒度调用是大量小请求，GPU 上每次 launch 的固定开销
                    # 反而更贵，而 CPU 实测 RTF≈0.03 已经远快过 diarization 本身。
                    # **2026-07-27 实测坐实了这条**（1s 帧 20 次均值）：
                    #   cpu  num_threads=1  23.4ms   cpu num_threads=2  19.4ms
                    #   cpu  num_threads=4  19.3ms   **cuda 83.2ms（慢 3.5×）**
                    # 别把它挪上 GPU。num_threads 也别往上加（2→4 没有收益），
                    # 门控这块的并行度在 `_gate_spans` 的 GATE_JOBS 那一层，不在这里。
                    provider="cpu",
                ),
                labels=AT_LABELS,
                top_k=527,
            )
            _at_speech_idx, _at_nonspeech_idx = speech, nonspeech
            _at = sherpa_onnx.AudioTagging(cfg)
            if missing:
                print(f"[voiceprint/gate] {missing} 个人声类名在标签表里没匹配上（bundle 换过？）", flush=True)
        except Exception as e:  # noqa: BLE001 — 门控挂了不该让整个 diarize 挂
            _at_error = f"{type(e).__name__}: {e}"
            print(f"[voiceprint/gate] 分类器不可用，本次退回不门控: {_at_error}", flush=True)
            _at = None
    return _at


def _gate_ready() -> bool:
    return os.path.exists(AT_MODEL) and os.path.exists(AT_LABELS)


if GATE:
    # 预拉：不阻塞导入，也不让 /health 背这个锅。
    threading.Thread(target=_load_tagger, daemon=True).start()


# --- DFN 前置清洗：二进制获取与调用 ------------------------------------------------
# 门控扔掉的是**整帧非人声**，但它是二值的：一帧只要人声主导就整帧留下，帧里那层盖在
# 人声上面的掌声/音乐/嘶声一起进了嵌入模型。门控治「这一秒是不是人在说话」，治不了
# 「这一秒的人声上面还盖着什么」。DFN（DeepFilterNet3，频域降噪）补的正是后半句。
#
# 两件事正交，所以**必须组合、且顺序固定为先门控后 DFN**：
#   - 单独用 DFN 有害（实证）：它对「整块没有一个字」的输入硬造伪人声嵌入——纯掌声段
#     3 段里 2 段「人声占比」反而升高，是个新污染源；块间自洽 p05 也比原声更差。
#     门控先把这些块排除掉，正好就是那两个失败模式的发生地，组合后它们消失。
#   - 反过来（先降噪再门控）会让分类器去判一段已被模型改写过的音频，门控那套实测阈值
#     全部作废。
# 效果由两轮量化 + 用户人耳终审拍板（DFN 明显好过 Demucs，轻微发闷可接受），
# 数据在 docs/research/voiceprint-clustering.md「便宜分离方案已评」。
#
# 为什么是官方 Rust CLI，不是 python 侧的 ONNX，也不是 torch：
#   1. **这个镜像没有 onnxruntime**。sherpa-onnx 把它作为私有 C++ 库带在自己的 wheel 里，
#      `import onnxruntime` 是 ModuleNotFoundError。
#   2. 就算加上它也没用：DFN 的 ONNX 导出**只含三个网络**（encoder/erb_dec/df_dec），
#      外面那一整套 DSP（ERB 滤波器组、STFT/ISTFT、复数域 deep filtering、重采样）
#      在官方实现里是 Rust（libDF）写的，走 ONNX 就得用 numpy 复刻一遍——正是门控头注
#      里拒绝 PANNs 的那个坑，而且这次更糟：门控复刻错顶多门控失准，这里复刻错会
#      **静默污染写进声纹库的向量**。
#   3. torch CPU 装完约 800MB。这个二进制给镜像加 **0 字节**（落缓存卷，见下）。
# 官方 release 的 musl 静态二进制自带 DeepFilterNet3 权重（不带 -m 直接跑，无运行时下载），
# 推理走 tract（纯 Rust ONNX 运行时）。DSP 用的就是参考实现本身。
#
# 采样率：DFN3 内部工作在 48k，我们的音频是 16k，但**这个二进制自己会重采样**——实测
# 直接喂 16k 的输出 vs 喂 48k 再降回 16k，corr 0.9945 / lag 0，且 120s 素材上两种喂法
# 耗时相同。所以直接喂 16k，不做 ffmpeg 往返（少两次进程、少两次重采样损耗）。
#
# ⚠⚠ **缺省关，因为它在这个嵌入模型上是净负的——实测，不是保守**。整集 A/B（E02，
# 一次 diarize 两次 regroup）三组数：
#
#   | | DFN 关 | 开 |
#   |---|---|---|
#   | 异人上界（对认名门 0.85） | 0.735（余量 0.115） | 0.773（余量 0.077） |
#   | must-link p95（对合并阈值 0.25） | 0.204 | 0.227 |
#   | >=30s 簇的覆盖时长占比 | 90.9% | 82.1% |
#   | 已认名者 vs 库内声纹 | 0.989 / 0.991 | 0.932 / 0.923 |
#
# 而可见层**一点没换来**：三条文本锚定真值的集中度逐字不变（71%/99%/97%），庞博与黄渤
# 照旧同簇。机制是「降噪连说话人细节一起压掉，且压法与内容有关」——所以同一个人的两份
# 样本各自被扭得不一样、彼此漂开（跨窗合并碎掉），不同的人则一起被推向某种"降噪后的
# 平均嗓音"（异人上界抬高）。
#
# **衰减上限扫过一遍，没有任何工作点比不洗更好**（五段连贯人声对照，每段劈两半模拟
# 同一个人的两份样本；刀口 = 同人最低相似度 − 异人上界）：
#
#   不洗 0.222 | 100dB 0.166 | 40dB 0.182 | 25dB 0.200 | 15dB 0.210 | 8dB 0.213
#
# 单调：洗得越轻越接近不洗，**极限就是不洗**。所以这不是调参能救的，别再去扫阈值。
# 人耳终审说 DFN 听起来更干净是对的——但嵌入模型不按人耳的标准给分。
#
# 代码留着（连同实测数据）是为了让下一个人不必重跑一遍：换了嵌入模型之后这笔账要重算，
# 那时把它打开重量即可。全过程见 docs/superpowers/specs/2026-07-27-voiceprint-dfn-preclean-design.md。
DFN = os.environ.get("VOICEPRINT_DFN", "0") != "0"
# 单进程单线程、峰值 RSS 实测 87MB（120s 素材；生产 clip <=20s 更低）。容器 16 核、
# 4GiB 上限、常驻约 1GB，开 4 个子进程绰绰有余。串行整集 +153s，4 路并行后每窗约 1.2s。
DFN_JOBS = int(os.environ.get("VOICEPRINT_DFN_JOBS", "4"))
# 洗涤区的成形参数，见 `_dfn_regions_for`。默认值不是调出来的，是「给流式模型足够上下文」
# 这个物理约束的保守取值：DFN3 帧长 20ms，1s 上下文 = 50 帧，够它把噪声估计收敛。
DFN_CONTEXT_S = float(os.environ.get("VOICEPRINT_DFN_CONTEXT_S", "1.0"))
DFN_MERGE_GAP_S = float(os.environ.get("VOICEPRINT_DFN_MERGE_GAP_S", "3.0"))
DFN_VERSION = "0.5.6"
DFN_DIR = os.environ.get("VOICEPRINT_DFN_DIR", "/root/.cache/deep-filter")
DFN_BIN = f"{DFN_DIR}/deep-filter-{DFN_VERSION}"
DFN_URL = os.environ.get(
    "VOICEPRINT_DFN_URL",
    f"https://github.com/Rikorose/DeepFilterNet/releases/download/v{DFN_VERSION}"
    f"/deep-filter-{DFN_VERSION}-x86_64-unknown-linux-musl",
)

_dfn = None
_dfn_lock = threading.Lock()
_dfn_error = None


def _fetch_dfn():
    """把 deep-filter 拉到缓存卷。已存在则直接返回。

    与 `_fetch_tagging_model()` 同一形状：先下到临时文件、chmod 完再整体 rename——
    半个二进制留在缓存里比没有更糟（下次启动会以为它在，然后在执行时炸）。
    权重烤在二进制里，所以「拉模型」和「拉可执行文件」是同一次下载。
    """
    if os.path.exists(DFN_BIN):
        return
    os.makedirs(DFN_DIR, exist_ok=True)
    fd, staging = tempfile.mkstemp(dir=DFN_DIR, prefix=".staging-")
    os.close(fd)
    try:
        urllib.request.urlretrieve(DFN_URL, staging)
        os.chmod(staging, 0o755)
        os.rename(staging, DFN_BIN)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)


def _load_dfn():
    """返回可执行的 deep-filter 路径；不可用时返回 None 并把原因记在 _dfn_error。"""
    global _dfn, _dfn_error
    if not DFN:
        return None
    if _dfn is not None:
        return _dfn
    with _dfn_lock:
        if _dfn is not None:
            return _dfn
        try:
            _fetch_dfn()
            # 真跑一次 --version：文件在盘上不等于能执行（架构不对/权限/musl 损坏都在这里现形）
            subprocess.run([DFN_BIN, "--version"], capture_output=True, check=True, timeout=60)
            _dfn = DFN_BIN
        except Exception as e:  # noqa: BLE001 — DFN 挂了不该让整个 diarize 挂
            _dfn_error = f"{type(e).__name__}: {e}"
            print(f"[voiceprint/dfn] 降噪不可用，本次退回不清洗: {_dfn_error}", flush=True)
            _dfn = None
    return _dfn


def _dfn_ready() -> bool:
    return os.path.exists(DFN_BIN)


if DFN:
    # 预拉：与门控同理由，别让第一次 /diarize 等一个 36MB 的下载。
    threading.Thread(target=_load_dfn, daemon=True).start()


def _dfn_regions_for(plan, sr, n_samples):
    """把「要取的区间」合并成**连续的洗涤区**（采样下标，左闭右开）。

    两步，都是为了让 DFN 拿到连续的自然音频：
      1. 间隔小于 DFN_MERGE_GAP_S 的相邻区间合成一个区（**连间隙一起洗**）——
         间隙里那点掌声/笑声正是它该拿来估噪声的东西，不是要躲开的东西；
      2. 每个区左右各加 DFN_CONTEXT_S 的上下文再洗，之后把上下文切掉。
         DFN3 是带状态的流式模型，开头几帧噪声估计还没收敛；1s 的区间如果不给
         前摇，整段都落在预热期里。
    """
    if not plan:
        return []
    # 下标算法必须与 `_concat_clip` 取音频时逐字一致，否则右边界能差一个采样、
    # 让「区间被洗涤区包住」的检查在整段音频末尾那一段上失败
    idx = sorted((int(s * sr), int(s * sr) + int((e - s) * sr)) for s, e in plan)
    gap = int(DFN_MERGE_GAP_S * sr)
    merged = [list(idx[0])]
    for a, b in idx[1:]:
        if a - merged[-1][1] < gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    ctx = int(DFN_CONTEXT_S * sr)
    return [(max(0, a - ctx), min(n_samples, b + ctx)) for a, b in merged]


def _dfn_wash_regions(audio, sr, plans):
    """把若干说话人的取样计划一次洗完。

    `plans`: [(key, [(start_s, end_s), ...])]，返回 {key: 洗过的拼接波形}。
    洗不成（二进制不可用 / 出岔子）返回 None，调用方退回不洗。

    **洗的是连续区，不是拼好的 clip**——这一条是实测定的，不是设计偏好：
    在五段连贯人声对照上（无拼缝、无门控介入，DFN 能拿到的最好条件）DFN 让异段相似度
    中位**降低** 0.023（可分性变好）；而拿「门控帧拼完再洗」跑整集 A/B，异人上界从
    0.735 涨到 0.784、>=30s 簇覆盖从 90.9% 掉到 81.7%。同一个模型、同一份音频，
    差别只在喂进去的是连续音频还是拼接音频——拼缝是宽带瞬变，在流式降噪器眼里就是噪声。
    """
    empty = {k: np.empty(0, dtype="float32") for k, _ in plans}
    # 去重：两个说话人的取样计划可能落进同一个洗涤区，同一段音频没必要洗两遍
    regions = sorted({r for _, plan in plans for r in _dfn_regions_for(plan, sr, len(audio))})
    if not regions:
        return empty
    washed = _dfn_clean([audio[a:b] for a, b in regions], sr)
    if washed is None:
        return None
    lookup = dict(zip(regions, washed))

    out = {}
    for key, plan in plans:
        parts = []
        for start, end in plan:
            a = int(start * sr)               # 与 `_concat_clip` 同一套下标算法，见那里的 ⚠
            b = a + int((end - start) * sr)
            if b <= a:
                continue
            # 找到包住这一段的洗涤区，从里面按相对下标取回来
            host = next((r for r in lookup if r[0] <= a and b <= r[1]), None)
            if host is None:
                return None  # 区间没被任何洗涤区包住 = 上面的合并逻辑有 bug，宁可整体退回
            parts.append(lookup[host][a - host[0]: b - host[0]])
        out[key] = np.concatenate(parts) if parts else np.empty(0, dtype="float32")
    return out


def _dfn_clean(clips, sr):
    """把若干段波形各洗一遍降噪，返回等长的列表（一一对应）；**洗不成返回 None**。

    降级而不是报错：二进制不可用 / 任何一步出岔子，调用方退回完全不洗。理由与门控相同——
    宁可脏，也不要因为一个辅助模型挂了就整条采集链路停摆。这里的口径风险是可控的：
    二进制是**导入时拉一次**，所以失败模式是「整个进程一直不洗」，不是「洗一半」，
    同一次 identify 内口径始终自洽。

    **要么全洗成功、要么全不洗**（中途失败返回 None，绝不返回半洗的混合体）：
    一半洗过一半没洗，跨窗量到的就是两种口径的差而不是「是不是同一个人」——
    门控 spec §7 记着这个坑，混口径把 p95 从 0.204 抬到 0.324。
    """
    binp = _load_dfn()
    if binp is None:
        return None
    if not clips:
        return []
    try:
        with tempfile.TemporaryDirectory(prefix="dfn-") as work:
            ind, outd = os.path.join(work, "in"), os.path.join(work, "out")
            os.makedirs(ind)
            os.makedirs(outd)
            names = []
            for i, c in enumerate(clips):
                n = f"c{i:04d}.wav"
                # FLOAT 而不是默认的 PCM_16：省掉一次 16bit 量化往返（两种实测都能读）
                sf.write(os.path.join(ind, n), c, sr, subtype="FLOAT")
                names.append(n)

            # 每个子进程领一批文件，一次调用洗完——固定开销 0.25s/次调用，批量能把它摊掉
            # （实测只省固定开销那一点，所以分批按并行度切就够，不必设计更复杂的批处理）。
            jobs = max(1, min(DFN_JOBS, len(names)))
            batches = [names[i::jobs] for i in range(jobs)]

            def run(batch):
                if not batch:
                    return
                subprocess.run(
                    [binp, "-o", outd, *(os.path.join(ind, n) for n in batch)],
                    capture_output=True,
                    check=True,
                )

            with futures.ThreadPoolExecutor(max_workers=jobs) as ex:
                for _ in ex.map(run, batches):
                    pass

            out = []
            for i, c in enumerate(clips):
                d, osr = sf.read(os.path.join(outd, names[i]), dtype="float32")
                # 长度实测逐样本相等；仍然核一遍，因为「悄悄变短一截」会静默改掉代表
                if osr != sr or abs(len(d) - len(c)) > sr // 100:
                    raise RuntimeError(f"dfn output shape mismatch: {len(d)}@{osr} vs {len(c)}@{sr}")
                out.append(d[: len(c)])
            return out
    except Exception as e:  # noqa: BLE001
        print(f"[voiceprint/dfn] 清洗失败，本窗退回不清洗: {type(e).__name__}: {e}", flush=True)
        return None


def _frame_scores(clip: np.ndarray, sr: int):
    """一帧的 (人声概率和, 非人声概率和)。多标签 sigmoid 输出，求和不归一。"""
    tagger = _load_tagger()
    if tagger is None:
        return None
    stream = tagger.create_stream()
    stream.accept_waveform(sample_rate=sr, waveform=clip)
    speech = nonspeech = 0.0
    for ev in tagger.compute(stream):
        if ev.index in _at_speech_idx:
            speech += ev.prob
        elif ev.index in _at_nonspeech_idx:
            nonspeech += ev.prob
    return speech, nonspeech


def _span_frames(audio, sr, start, end, frame_n):
    """这个段会被切成哪些帧，返回 [(起样本, 止样本)]。

    刀口从**段起点**起步（不是全窗固定网格，理由见 `_gate_spans` 头注），
    不足半帧的尾巴丢掉——分类器在过短音频上不可信，而它值不了几毫秒。

    帧边界只由 (start, end, frame_n) 决定、与「哪帧过了门控」无关，所以可以先枚举再并行。
    """
    out = []
    t = start
    while t < end - 1e-9:
        a = int(t * sr)
        b = min(a + frame_n, int(end * sr), len(audio))
        if b - a < frame_n // 2:
            break
        out.append((a, b))
        t = b / sr
    return out


_gate_pool_lock = threading.Lock()
_gate_pool_ref = None


def _gate_pool():
    """逐帧分类用的线程池——**进程级单例**，不是每次调用新建一个。

    原来是每次 `_gate_spans` 都 new 一个（整集 222 次），每个再 `shutdown(wait=False)`。
    功能上没错，但一次识别里就要造 222 个池、上千次线程起落，而这个容器还跑在
    `uvicorn --reload` 下（源码 bind-mount，改文件就重启）——reload 撞上正在跑的请求时，
    这些池的收尾正好是最容易卡住 shutdown 的东西。实测过一次容器卡死：最后一行日志是
    成功的 /diarize，之后 /health 20 秒不应答。改成单例后线程总数恒定为 GATE_JOBS。
    """
    global _gate_pool_ref
    if GATE_JOBS <= 1:
        return None
    if _gate_pool_ref is None:
        with _gate_pool_lock:
            if _gate_pool_ref is None:
                _gate_pool_ref = futures.ThreadPoolExecutor(
                    max_workers=GATE_JOBS, thread_name_prefix="gate"
                )
    return _gate_pool_ref


def _gate_spans(audio, sr, spans, budget_s):
    """把一个说话人的若干段逐帧过门控，返回 (保留下来的区间列表, 总秒数)。

    段按**长的优先**扫（与 `_concat_clip` 同一理由：长段的音频更可信，先把预算花在
    它上面），攒够 budget_s 秒干净音频就停——常态下每人只需过 20 多秒音频，不是整窗。
    只有「怎么扫都攒不够」的说话人才会被扫完全部段，而那正是要弃权的那种。

    门控不可用（模型没拉下来 / 加载失败）时返回 None，调用方退回不门控的老行为——
    宁可脏，也不要因为一个辅助模型挂了就整条采集链路停摆。

    **并行怎么做的，以及为什么不是「全窗共用帧网格」**（2026-07-27 实测，别重摸）：
    分类器成本几乎全按**音频秒数**走——拟合 `0.1ms 固定 + 16.6ms/秒`，1s 帧里固定开销
    只占 1%。所以能省的是秒数，不是调用次数。三条路量下来：

    - **全窗共用网格**（整窗按固定 1s 格子标一遍、各说话人查表）：E02 实测要标 4174s，
      按需扫只要 2432s，**贵 1.72×**；能省的跨说话人段重叠只有 **2.9%**，去重的天花板
      就这么点。它顺带还会放宽门控（段边界的余料不再整帧丢弃 → 弃权 91→72、庞博集中度
      99%→87%），账记在 spec `2026-07-27-voiceprint-frame-cut-design.md` §4。
      那一轮记下的「4× 提速」其实是**并行**给的，不是网格给的。
    - **加长帧一次多问**：不行，帧长必须是 1s（GATE 头注：0.5s 更糊，长了跨事件）。
    - **并行**：4 线程实测 2.97×（8 线程 3.49×，边际递减）。就是它。

    唯一的顺序依赖是「攒够 budget_s 就停」，所以按批走：一批 GATE_JOBS 帧并行分类，
    再**逐帧按序**消费。预算在批中途满了，这批余下的帧白算（≤ jobs-1 帧，和有用的帧
    同批并行、不多花墙钟），下一批不再取。因此结果与顺序版**逐位相同**——
    并行只改墙钟，不改任何门控判定。

    取批**跨段**（`planned` 是所有段拉平后的惰性序列，不是每段各凑一批）：按段凑批时
    段短的人永远凑不满一批，同一份素材实测只有 2.25×，跨段取批 2.63×。
    惰性是承重的——它保证「预算满了就不再往下算」这条早停仍然有效。

    **验收**（E02 全集 35 窗 / 222 个说话人，`jobs=4`）：kept 区间与秒数与顺序版
    **222/222 逐位相同**，门控后处理 40–42s → 13–15s（两个 harness 各测一次，2.7–3.1×，
    要复跑用 `scripts/voiceprint-gate-parity.py`）。逐位相同是这条改动的
    全部安全性依据——它不改任何判定，所以不需要再验下游那三条锚定真值。
    共享 tagger 的线程安全另有实证：同 64 帧顺序 vs 4 线程，分数逐位相同
    （竞争会静默把脏向量写进声纹库，所以这条必须实证，不能靠「ORT 应该是线程安全的」）。
    """
    if not GATE or _load_tagger() is None:
        return None
    frame_n = max(1, int(GATE_FRAME_S * sr))
    kept = []
    total = 0.0
    planned = (
        ab
        for start, end in sorted(spans, key=lambda s: -(s[1] - s[0]))
        for ab in _span_frames(audio, sr, start, end, frame_n)
    )
    pool = _gate_pool()
    try:
        while total < budget_s:
            batch = list(itertools.islice(planned, GATE_JOBS))
            if not batch:
                break
            if pool is None:
                scores = [_frame_scores(audio[a:b], sr) for a, b in batch]
            else:
                scores = list(pool.map(lambda ab: _frame_scores(audio[ab[0] : ab[1]], sr), batch))
            for (a, b), sc in zip(batch, scores):
                if sc is None:
                    return None
                speech, nonspeech = sc
                if speech > GATE_SPEECH_MIN and speech > GATE_SPEECH_RATIO * nonspeech:
                    f0, f1 = a / sr, b / sr
                    if kept and abs(kept[-1][1] - f0) < 1e-6:
                        kept[-1] = (kept[-1][0], f1)  # 与上一帧相接 → 合成一段
                    else:
                        kept.append((f0, f1))
                    total += f1 - f0
                if total >= budget_s:
                    break
    finally:
        pass  # 池是进程级单例，不在这里关（见 _gate_pool）
    return kept, total


# --- 非人声碎片：给「掌声变成的说话人」提供帧证据 --------------------------------
# 修的是一个用户可见的错：观众的笑声/掌声被分段模型判成独立说话人，堂堂正正出现在名单里
# 叫「说话人 14」。E02 有 7 个这样的簇（155s），用户逐段听完 52 段确认 **88% 是纯掌声**
# （标注留档 `data/voiceprint-spike/e02-shard-labels.json`）。
#
# **这里只出帧证据，不做判定。** 判定要两个信号都命中，而另一个信号（结构：这个簇只活在
# 某人的发言里、与他 0 秒间隔高频交替、**在别处从不露面**）需要**整条录音**才看得出来，
# 而本容器一次只看一个 120s 的窗。实测代价很具体：判据放在窗内只摘到 41s，放在跨窗合并后的
# 完整时间线上能看见 19 个碎片簇。所以结构那一半在 TS 侧（`src/voiceprint/shards.ts`），
# 由它圈出嫌疑区间、回来问这里要帧证据（`POST /speech-frac`）。
#
# ⚠ 同类改动 2026-07-27 试过一次并被证伪整体撤回（拿帧标签切整条时间线，切掉的 47-69% 是
# 压着音乐讲话的真人，spec `2026-07-27-voiceprint-frame-cut-design.md`）。教训是硬的：
# **门控误判 = 少点干净音频（可恢复）；切分误判 = 一个人说过话这个事实消失（不可恢复）。**
# 所以阈值只用**低端**：人声帧占比 <= SHARD_SPEECH_MAX。用户对表实测这一档 **21/21 全是
# 噪声**；>=50% 那档 4 噪声 vs 3 混合，不可信。只命中一个信号的一律留着。
#
# **帧占比只数完整落在段内的帧**：拿「盖住它的那一整帧」算过一次，1 秒的帧里大半是旁边
# 演员的说话声，于是纯掌声的 0.34s 段显示 100% 人声。不足一帧 → 判「无帧证据」→ 不删。
SHARD_SPEECH_MAX = float(os.environ.get("VOICEPRINT_SHARD_SPEECH_MAX", "0.25"))


def _speech_frac(audio, sr, a, b):
    """[a,b] 内**完整**的 1s 帧里，人声主导的占比。凑不出一整帧 → None（无帧证据）。"""
    frame_n = max(1, int(GATE_FRAME_S * sr))
    i0 = int(math.ceil(a / GATE_FRAME_S))
    i1 = int(b // GATE_FRAME_S)
    idx = [i for i in range(i0, i1) if i * frame_n + frame_n <= len(audio)]
    if not idx:
        return None
    hit = 0
    for i in idx:
        sc = _frame_scores(audio[i * frame_n: i * frame_n + frame_n], sr)
        if sc is None:
            return None
        speech, nonspeech = sc
        if speech > GATE_SPEECH_MIN and speech > GATE_SPEECH_RATIO * nonspeech:
            hit += 1
    return hit / len(idx)


def _read_audio(raw: bytes):
    # ffmpeg-decode arbitrary media (mp4/m4a/wav) -> 16k mono float32
    with tempfile.NamedTemporaryFile(suffix=".bin") as f:
        f.write(raw)
        f.flush()
        out = subprocess.run(
            ["ffmpeg", "-i", f.name, "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
            capture_output=True,
            check=True,
        ).stdout
    data, sr = sf.read(io.BytesIO(out), dtype="float32")
    return data, sr


def _compute_embedding(clip: np.ndarray, sr: int):
    if len(clip) == 0:
        return []
    stream = _embed.create_stream()
    stream.accept_waveform(sample_rate=sr, waveform=clip)
    stream.input_finished()
    emb = _embed.compute(stream)
    return list(map(float, emb))


def _clip_plan(spans, cap_s):
    """`_concat_clip` 实际会取哪几段音频（长的优先，攒到 cap_s 为止）。

    单独拆出来是因为 DFN 需要**在拼接之前**知道要取哪些区间——它必须洗连续音频，
    拼完再洗等于让一个流式模型在一路拼缝里估噪声（见 `_dfn_wash_regions` 头注）。
    """
    out = []
    total = 0.0
    for start, end in sorted(spans, key=lambda s: -(s[1] - s[0])):
        take = min(end - start, cap_s - total)
        if take <= 0:
            break
        out.append((start, start + take))
        total += take
    return out, total


def _concat_clip(audio, sr, spans, cap_s):
    """把一个说话人的若干段音频拼起来（长的优先），拼到 cap_s 为止。

    长的优先而不是按时间顺序：越长的段嵌入越稳（见 REGROUP 头注的块长表），
    先把预算花在最可信的音频上。返回 (波形, 实际拼了多少秒)。
    """
    plan, total = _clip_plan(spans, cap_s)
    parts = []
    for start, end in plan:
        # ⚠ 必须是 `int(start*sr) + int(take*sr)`，不是 `int(end*sr)`——两者会差一个采样，
        # 而这条路（DFN 关）要与门控轮的基线逐字节一致。差一个采样实测能让整集少出一个
        # 窗内说话人（223 → 222），基线就对不上了。
        a = int(start * sr)
        b = a + int((end - start) * sr)
        if b > a:
            parts.append(audio[a:b])
    if not parts:
        return np.empty(0, dtype="float32"), 0.0
    return np.concatenate(parts), total


def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n > 0 else None


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def _average_linkage(vecs, threshold):
    """平均连接凝聚：最近的一对先并，簇间距离取跨簇全部配对的均值，超过 threshold 就停。

    没有输入顺序依赖（并列时按下标小者优先，结果对同一输入确定）。n 是一窗内的说话人数
    （个位数），朴素实现足够。与 TS 侧 `src/voiceprint/windowed.ts` 的跨窗合并同一套语义——
    两处都用平均连接，是为了让「窗内」和「跨窗」对『同一个人』的判定标准一致。
    """
    n = len(vecs)
    labels = list(range(n))
    if n <= 1:
        return labels
    dsum = [[0.0] * n for _ in range(n)]
    dcnt = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            dsum[i][j] = 1 - _cos(vecs[i], vecs[j])
            dcnt[i][j] = 1
    alive = list(range(n))
    members = {i: [i] for i in range(n)}
    while len(alive) > 1:
        best = None
        best_d = None
        for x in range(len(alive)):
            for y in range(x + 1, len(alive)):
                a, b = alive[x], alive[y]
                d = dsum[a][b] / dcnt[a][b]
                if best_d is None or d < best_d:
                    best_d, best = d, (a, b)
        if best is None or best_d > threshold:
            break
        a, b = best
        for o in alive:
            if o in (a, b):
                continue
            ka = (a, o) if a < o else (o, a)
            kb = (b, o) if b < o else (o, b)
            dsum[ka[0]][ka[1]] += dsum[kb[0]][kb[1]]
            dcnt[ka[0]][ka[1]] += dcnt[kb[0]][kb[1]]
        members[a] = members[a] + members[b]
        del members[b]
        alive.remove(b)
    for ci, (_, group) in enumerate(sorted(members.items())):
        for m in group:
            labels[m] = ci
    return labels


def _abstains(gated_s, raw_s):
    """这个说话人本窗该不该弃权（不出干净代表）。

    ⚠ 两个条件是 **AND**，缺一不可，理由是实测教训（E02，2026-07-26）：

    只用「干净人声 < 8s」这一条会把**本来就只说了几句**的人一起弃权掉——E02 一窗里
    261 个窗内说话人有 150 个（57%）原始音频就不到 8 秒，他们的音频**是干净的**，
    只是短。全弃权的后果实测是可见层变差：名单 15→12 人、林简七的 set 从 2 个簇碎成 3 个。
    门控要治的是「脏」，不是「短」；短由跨窗那一侧自己的锚点门槛管
    (`src/voiceprint/windowed.ts` 的 MIN_ANCHOR_S)，两件事别混成一个阈值。

    所以再加一条「而且大部分音频是被门控扔掉的」：`gated < ratio × raw`。
    - raw=20s, gated=5s  → 四分之三是掌声笑声 → 弃权（这才是要拦的）
    - raw=5s,  gated=4.5s → 干净、只是短 → 照常出代表，交给下游的时长门槛
    实测：真正「有 8 秒以上原始音频、却被门控判脏」的只有 32/261（12%），
    正是该弃权的那一批。
    """
    return gated_s < GATE_MIN_CLEAN_S and gated_s < GATE_ABSTAIN_RATIO * raw_s


def _speaker_clip(audio, sr, spans, cap_s):
    """一个说话人拿去算代表的音频。门控开着 → 只收人声帧；关着/不可用 → 原样拼接。

    返回 (波形, 秒数, 门控保留的区间列表 or None)。第三项是 None 就代表「这次没门控」，
    后面所有分支都靠它区分新老行为——关掉门控时本函数与 `_concat_clip` 逐字节等价。
    """
    gated = _gate_spans(audio, sr, spans, cap_s)
    if gated is None:
        clip, secs = _concat_clip(audio, sr, spans, cap_s)
        return clip, secs, None
    intervals, secs = gated
    clip, _ = _concat_clip(audio, sr, intervals, cap_s)
    return clip, secs, intervals


def _regroup(audio, sr, raw_segments):
    """第二遍：按拼接音频重算的代表把第一遍的说话人重新分组。

    返回 (旧 speaker → 新 speaker 的映射, 每个新说话人的干净代表)。
    重算不动任何 segment 的时间边界——只改标签。

    开着帧级门控时（见 GATE 头注），「拼接音频」只收人声帧，且干净人声不足 MIN_REP_S
    的说话人**弃权**：不进 anchors（不参与决定分组）、不贡献任何 clean 代表。它的
    segments 照常返回，只是在 speakers[] 里以 `abstained: true` 出现。
    """
    spans = {}
    for s in raw_segments:
        spans.setdefault(s["speaker"], []).append((s["start"], s["end"]))

    reps = {}
    for spk, ss in spans.items():
        clip, secs, kept = _speaker_clip(audio, sr, ss, REP_CLIP_S)
        emb = _unit(_compute_embedding(clip, sr)) if len(clip) else None
        if emb is None and kept is not None:
            # 门控把这个人的音频全扔光了。仍要给他一个向量——否则他的 segments 会被
            # 整体丢弃、时间线就变了，而门控只该决定「谁贡献指纹」。用原始音频算一个
            # **只用来问路**的代表：它参与不了分组决策（secs=0 必然弃权），也进不了任何
            # 组的 clean 代表，唯一用途是让这堆段就近找个归宿。
            fallback, _ = _concat_clip(audio, sr, ss, REP_CLIP_S)
            emb = _unit(_compute_embedding(fallback, sr)) if len(fallback) else None
        if emb:
            reps[spk] = (emb, secs, kept)

    gated_ran = any(k is not None for (_, _, k) in reps.values())
    anchors = [k for k, (_, secs, _) in reps.items() if secs >= MIN_REP_S]
    # 门控没跑时的老行为：极短窗里全都够不上门槛 → 所有人当锚点，否则一个组都出不来。
    # 门控跑了就**不给**这条退路——「本窗没人有 8 秒干净人声」是一个真实答案，
    # 拿脏音频硬凑出锚点正是要拦的事。
    if not anchors and not gated_ran:
        anchors = list(reps.keys())
    anchors.sort()
    anchor_set = set(anchors)

    labels = _average_linkage([reps[k][0] for k in anchors], REGROUP_THRESHOLD) if anchors else []
    mapping = {spk: labels[i] for i, spk in enumerate(anchors)}

    # 组质心（按各自拼到的秒数加权）——碎片就近归附时比对的就是它。
    # 只有锚点进质心：弃权者的音频不配定义任何人长什么样。
    csum = {}
    for spk in anchors:
        emb, secs, _kept = reps[spk]
        g = mapping[spk]
        acc = csum.setdefault(g, [0.0] * len(emb))
        for i, v in enumerate(emb):
            acc[i] += v * secs
    centroids = {g: _unit(v) for g, v in csum.items()}

    for spk, (emb, _secs, _kept) in reps.items():
        if spk in mapping:
            continue
        best_g, best_d = None, None
        for g, cv in centroids.items():
            d = 1 - _cos(emb, cv)
            if best_d is None or d < best_d:
                best_d, best_g = d, g
        # 太远就自成一组（可能真是个只说了两句的新人）
        mapping[spk] = best_g if best_g is not None and best_d <= REGROUP_THRESHOLD else max(mapping.values(), default=-1) + 1

    # 新说话人按首次出现时间重编号，SPEAKER_00 = 本窗第一个开口的人
    first = {}
    for s in raw_segments:
        g = mapping.get(s["speaker"])
        if g is None:
            continue
        if g not in first or s["start"] < first[g]:
            first[g] = s["start"]
    order = sorted(first, key=lambda g: (first[g], g))
    renumber = {g: i for i, g in enumerate(order)}
    out_map = {spk: f"SPEAKER_{renumber[g]:02d}" for spk, g in mapping.items() if g in renumber}

    # 每个新说话人的干净代表：把它名下全部旧说话人的（门控后的）音频再拼一次重算。
    # 门控没跑时收原始音频，与历史行为逐字节一致。
    clean = {}
    gated_secs = {}
    raw_secs = {}
    for spk, new in out_map.items():
        if gated_ran:
            gated_secs[new] = gated_secs.get(new, 0.0) + reps[spk][1]
            raw_secs[new] = raw_secs.get(new, 0.0) + min(sum(e - s for s, e in spans[spk]), REP_CLIP_S)
            src = reps[spk][2]
            if src is None:
                continue
        else:
            src = spans[spk]
        clean.setdefault(new, []).extend(src)

    # 先把每个人的 clip 拼出来、并把弃权判掉，**再**统一洗——弃权只看秒数、与音质无关
    # （`_abstains` 吃的是 gated/raw 秒数），所以 DFN 改变不了谁弃权，弃权者的 clip 也就
    # 不必浪费一次清洗：它的 embedding 反正要被清成 []。
    rows = []
    plans = {}
    for new in sorted(set(out_map.values())):
        ss = clean.get(new) or []
        plan, secs = _clip_plan(ss, REP_CLIP_S)
        plans[new] = plan
        clip, _ = _concat_clip(audio, sr, ss, REP_CLIP_S) if ss else (np.empty(0, dtype="float32"), 0.0)
        row = {"speaker": new, "embedding": [], "clip_seconds": round(secs, 2)}
        abstained = False
        if gated_ran:
            g, r = gated_secs.get(new, 0.0), raw_secs.get(new, 0.0)
            row["gated_seconds"] = round(g, 2)
            abstained = _abstains(g, r)
        rows.append([row, clip, abstained, new])

    # DFN 前置清洗（见 DFN 头注）。**只在门控真跑过的时候洗**，这一条是硬的：单独用 DFN
    # 实测有害（对「整块没有一个字」的输入硬造伪人声嵌入），它之所以在组合里安全，全靠
    # 门控已经把那种块排除掉了。门控没跑 → 没有那层保护 → 不洗。
    if gated_ran and DFN:
        live = [(new, plans[new]) for row, clip, ab, new in rows if len(clip) and not ab]
        washed = _dfn_wash_regions(audio, sr, live) if live else {}
        # None = 这一窗洗不成 → 整窗退回不洗，不出半洗的混合口径
        if washed is not None:
            for r in rows:
                if r[3] in washed:
                    r[1] = washed[r[3]]

    speakers = []
    for row, clip, abstained, _new in rows:
        if gated_ran and (abstained or not len(clip)):
            # 弃权：门控看过这个人的音频，判定它回答不了「这是谁」。
            # **必须显式说出来**——悄悄缺席在下游 (src/voiceprint/windowed.ts) 会被当成
            # 「老容器没给代表」而退回段级均值，那正是要拦的脏东西。
            row["abstained"] = True  # embedding 已经是 []
        else:
            row["embedding"] = _compute_embedding(clip, sr) if len(clip) else []
        speakers.append(row)
    return out_map, speakers


@app.get("/health")
def health():
    if ACTIVE_PROVIDER is None:
        _load()
    # 门控 / DFN 状态照实报：`ready` 只看资产在不在盘上（**不**在这里触发加载/下载——
    # compose 的 healthcheck 只给 5s，让它去等一个几百 MB 的下载会把容器判成 unhealthy
    # 并反复重启）。两者的下载都由导入时的后台线程负责。
    return {
        "ok": True,
        "provider": ACTIVE_PROVIDER,
        "model_version": MODEL_VERSION,
        "frame_gate": {"enabled": GATE, "ready": _gate_ready(), "error": _at_error},
        "dfn": {"enabled": DFN, "ready": _dfn_ready(), "error": _dfn_error},
    }


@app.post("/diarize")
async def diarize(file: UploadFile, hint: str = Form(HINT_DEFAULT)):
    # `hint` (accuracy|fast) is accepted for contract compatibility with the client; the
    # single baked model pair does not currently offer a faster alternative pipeline.
    if _diar is None:
        _load()
    audio, sr = _read_audio(await file.read())
    duration_s = len(audio) / sr
    # Reject before the expensive step (_diar.process, which is what OOM'd in production — see
    # header comment) rather than after. Decoding to a numpy array above is cheap even at this
    # duration (16k mono float32 ~= 64KB/s); the diarization pipeline is the big allocation, so
    # the cap has to land before that call to actually protect memory.
    if duration_s > MAX_SINGLE_S:
        raise HTTPException(
            status_code=413,
            detail=(
                f"audio duration {duration_s:.1f}s exceeds VOICEPRINT_MAX_SINGLE_S="
                f"{MAX_SINGLE_S:.0f}s — window it at the caller — see capability-job-runner spec"
            ),
        )
    cap = int(EMBED_CLIP_MAX_S * sr)
    segments = []
    result = _diar.process(audio).sort_by_start_time()
    for seg in result:
        clip = audio[int(seg.start * sr): int(seg.end * sr)][:cap]
        emb = _compute_embedding(clip, sr)
        segments.append({
            "start": float(seg.start),
            "end": float(seg.end),
            "speaker": f"SPEAKER_{seg.speaker:02d}",
            "embedding": emb,
        })
    # 第二遍：按拼接音频重算的代表重新分组（见 REGROUP 头注）。只改 speaker 标签，
    # 段的时间边界与段级 embedding 原样返回——调用方（TS 跨窗合并）拿到的形状不变。
    speakers = []
    if REGROUP and segments:
        t0 = time.time()
        remap, speakers = _regroup(audio, sr, segments)
        abstained = sum(1 for s in speakers if s.get("abstained"))
        print(
            f"[voiceprint/regroup] dur={duration_s:.0f}s speakers={len(speakers)} "
            f"abstained={abstained} gate={'on' if GATE else 'off'} wall={time.time() - t0:.1f}s",
            flush=True,
        )
        if remap:
            for s in segments:
                s["speaker"] = remap.get(s["speaker"], s["speaker"])
    # `speakers`：每个说话人一份**由拼接音频算出的干净代表**。调用方应优先用它，而不是
    # 拿一堆短段 embedding 求均值——均值抹不掉「每段音频太少」这个根子（块长表在头注）。
    return {"model_version": MODEL_VERSION, "segments": segments, "speakers": speakers}


@app.post("/speech-frac")
async def speech_frac(file: UploadFile, intervals: str = Form(...)):
    """给定区间，回答「这段里人声帧占比多少、算不算非人声」。见 SHARD_SPEECH_MAX 头注。

    `intervals` 是 JSON `[[起,止], ...]`（相对本次上传的音频，秒）。逐区间返回：
      `frac`  —— 完整落在区间内的 1s 帧里人声主导的占比；凑不出一整帧则为 null
      `nonspeech` —— `frac is not None and frac <= 阈值`。**判定只用低端**，
                     所以 null（无帧证据）一律 false：没有证据不等于是噪声。

    阈值留在这里、不由调用方传：帧模型和它的判据在同一侧，两处各写一份必然分叉。
    """
    if _load_tagger() is None:
        raise HTTPException(status_code=503, detail="frame gate model unavailable")
    try:
        want = json.loads(intervals)
        pairs = [(float(a), float(b)) for a, b in want]
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"bad intervals: {e}") from e
    audio, sr = _read_audio(await file.read())
    out = []
    for a, b in pairs:
        frac = _speech_frac(audio, sr, a, b)
        out.append({
            "start": a,
            "end": b,
            "frac": None if frac is None else round(frac, 4),
            "nonspeech": frac is not None and frac <= SHARD_SPEECH_MAX,
        })
    n = sum(1 for x in out if x["nonspeech"])
    print(f"[voiceprint/speech-frac] {len(out)} 个区间 → 判为非人声 {n} 个"
          f"（阈值 <={SHARD_SPEECH_MAX}）", flush=True)
    return {"threshold": SHARD_SPEECH_MAX, "items": out}


@app.post("/embed")
async def embed(file: UploadFile):
    if _embed is None:
        _load()
    audio, sr = _read_audio(await file.read())
    emb = _compute_embedding(audio, sr)
    return {"model_version": MODEL_VERSION, "embedding": emb}
