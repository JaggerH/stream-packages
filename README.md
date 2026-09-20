# stream-packages

Stream 可选包的家：**每个目录 = 一个容器镜像 + 一份 Stream 包清单**。这些包只提供一个后端容器
（去水印 / 文档解析 / 说话人分离），不带 Source、不带凭证；用户按需装，不装的人不为它付任何代价。

| 目录 | npm | 镜像 | GPU |
|---|---|---|---|
| `dewatermark/` | `@streamapp/dewatermark` | `ghcr.io/jaggerh/dewatermark` | 否 |
| `mineru/` | `@streamapp/mineru` | `ghcr.io/jaggerh/mineru-server` | 是 |
| `voiceprint/` | `@streamapp/voiceprint` | `ghcr.io/jaggerh/voiceprint-server` | 是（仅 GPU） |
| `whisper-asr/`、`whisperx-server/` | — | — | 仅存档，未接入 Stream |

## 目录约定

- `package.json#stream.backend` 是 Stream 读的**唯一契约**（image / port / health / gpu / env / volumes /
  mem / standby）。Stream 宿主拿它建容器、standby 管生灭；契约字段的定义见 Stream 仓库
  `docs/PACKAGE.md` 的容器槽位一节。
- `Dockerfile` 是镜像的**唯一来源**。workflow 里的镜像名从清单的 `image` 字段读，不另写一遍。
- npm 包只装 `package.json` + `README.md`（`files` 白名单）；Dockerfile / app.py 不进 npm 包，
  安装侧只认清单。
- 每个目录自带 README：接口、镜像、在 Stream 里的 backend 字段说明。

## 发版

一个 tag 同时锁定镜像与清单：

```bash
# 先把 <dir>/package.json 的 version 改成目标版本并提交（workflow 会核对，对不上就拒发）
git tag dewatermark-v1.0.1 && git push --tags
```

`.github/workflows/release.yml`：构建并推 `ghcr.io/…:<版本>` + `:latest`，再 `npm publish` 该目录
（版本已在 npm 上则跳过，tag 可以重打）。PR / 非 tag push 只 build 不 push、不发。
**首发**人工做一次 `npm publish --access public`（scope 首个版本的 public 设置），之后交给 tag。
需要仓库 secret `NPM_TOKEN`。

## 用户怎么装

```bash
stream add @streamapp/dewatermark     # 或 mineru / voiceprint
```

前提：

- Stream 配置里 `manage_containers: true`（宿主要能建容器，本机要有 docker）。
- GPU 包（mineru / voiceprint）要 nvidia container toolkit；voiceprint 没有 CPU 变体，无显卡的机器
  起不来。
- 装完后端重载即挂上；容器闲置到 `standby.idleMinutes` 自动回收，下次请求再唤醒。
