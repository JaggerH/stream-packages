# Dewatermark

去水印后端：一张图进、一张图出。在四个角找平台的 AI 水印标（模板匹配），用 LaMa（iopaint）把那一块
补成周围的样子。给 Stream 的 `/api/doubao/v1/images/generations` 那条路用（豆包网页版预览图左上角
的「AI 生成」）。Install with `stream add @streamapp/dewatermark`.

```
POST /remove   multipart image=<PNG|JPEG>  [?box=x,y,w,h 像素：跳过检测直接补这一块]  -> image/png
GET  /health                                                                        -> {"ok": true}
```

响应头 `x-dewatermark: matched | fallback`：一个模板都没匹配到时**回退左上固定框**并说出来——
静默回退等于下一个人以为检测一直在工作。

**CPU-only by design**：LaMa 在 CPU 上补一个 2048² 图角落的小块约 3–6s；这条链路一次 4 张、上游生图
本身就要 20s，GPU 省下的几秒换不来 `gpu: true` 在 CPU 主机上起不来的代价。模型烤进镜像
（`iopaint download`），运行期不下任何东西。

## 镜像

`ghcr.io/jaggerh/dewatermark`，由本目录的 `Dockerfile` 唯一定义，仓库 workflow 按 `dewatermark-v<版本>`
tag 构建推送。水印模板在 `templates/`（加模板的量法见那里的 README）。本地冒烟：`smoke.py`。

## backend

- `gpu: false` —— 见上；任何主机都起得来。
- `mem: 3G` —— LaMa + opencv 的 python 侧上限。
- `standby.idleMinutes: 10` —— 闲置十分钟自动回收。
- `env.DEWATERMARK_MATCH_MIN`（可选，默认 0.6）—— 模板匹配的最低分，低于它走回退框。
