# Voiceprint

sherpa-onnx diarization + speaker embedding backend (no credentials, no source).
Service-only package: contributes a backend container reached via the Stream backend at
`/_p/voiceprint`. Registers no source adapter. Install with `stream add @streamapp/voiceprint`.

GPU-ONLY（用户拍板 2026-07-23）：CPU 上 diarization ~0.9x 实时，2 小时综艺要跑近 2 小时，功能本身
失去意义——不做 CPU/GPU 双变体，镜像只出 CUDA 版（本目录 `Dockerfile`，轻量 slim + pip cu11 轮子方案，
同 whisper-asr）。代价：无 GPU 的主机起不了这个服务。

## 镜像

`ghcr.io/jaggerh/voiceprint-server`，由本目录的 `Dockerfile` 唯一定义，仓库 workflow 按
`voiceprint-v<版本>` tag 构建推送。GPU-only sherpa-onnx image: `package.json`'s `stream.backend`
sets `gpu: true`, so starting the container REQUIRES the nvidia container toolkit — a CPU-only host
cannot start this service at all. CUDA runtime libs come from pip wheels, `libcuda.so` itself comes
from the host driver via `--gpus`（WSL2: bind-mounted from `/usr/lib/wsl/lib`）。

本地改 `app.py` 想免重建镜像（bind-mount + `uvicorn --reload`）时两处不能想当然：

1. **base image 就复用烤好的那个** —— 依赖（sherpa-onnx +cuda 轮子、cudnn）和模型（`/app/models`，
   几百 MB）都在里面，换个干净 python 镜像等于要重装一遍。
2. **挂到 `/src` 而不是 `/app`** —— `app.py` 在 `/app`、模型也在 `/app/models`；把源码挂到 `/app`
   会把模型目录整个盖掉。代码里模型是绝对路径（`/app/models/...`），所以工作目录换到 `/src`
   照样解析得到。

## backend

- `gpu: true` —— GPU-only 镜像 ⇒ 宿主建容器时发 nvidia 设备预留（容器里 libcuda 来自宿主驱动）。
- `env.VOICEPRINT_HINT_DEFAULT` —— sherpa-onnx model bundle (segmentation + speaker embedding) baked
  into the image.
- `env.VOICEPRINT_USE_CUDA` —— GPU-only 镜像显式要 cuda provider。`app.py` 仍留 cuda→cpu 兜底（宿主
  GPU 抽风时降级慢跑，不至于整个能力挂掉），`/health` 的 provider 字段报的是实际生效的那个——盯它验真。
- `env.VOICEPRINT_MODEL_VERSION` —— Vector-comparability key: pinned to the baked
  segmentation+embedding model pair (3D-Speaker ERes2NetV2, Chinese zh-cn common, 16k, 192-dim).
  Bump only if the baked models change — old voiceprints of a different version are treated as
  incomparable.
- `env.VOICEPRINT_DFN` —— DFN 前置清洗试用开关（2026-07-27 证伪缺省关；用户手动开做端到端体验，
  随时可撤此项恢复）。
- `volumes` —— `voiceprint-cache:/root/.cache`，缓存跨重建保留。
- `mem: 4G`、`standby.idleMinutes: 10` —— 闲置十分钟回收，不用时不占资源。
