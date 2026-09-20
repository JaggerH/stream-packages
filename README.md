# stream-packages

Stream 可选包的家：**每个目录 = 一份 Stream 包清单 + 它填的那一格**。两种形状：
**容器包**（一个 Dockerfile，`package.json#stream.backend` 声明镜像，只提供一个后端容器——验证码识别 /
去水印 / 文档解析 / 说话人分离）和**代码包**（TypeScript 源码 tsdown 成 `dist/index.js`，
`package.json#stream.capability` 声明它，给模型交出几个动词——美团生活）。都不带 Source、不带凭证；
用户按需装，不装的人不为它付任何代价。

| 目录 | npm | 形状 | 镜像 | GPU |
|---|---|---|---|---|
| `ddddocr/` | `@streamapp/ddddocr` | 容器 | `ghcr.io/jaggerh/ddddocr-server` | 否 |
| `dewatermark/` | `@streamapp/dewatermark` | 容器 | `ghcr.io/jaggerh/dewatermark` | 否 |
| `mineru/` | `@streamapp/mineru` | 容器 | `ghcr.io/jaggerh/mineru-server` | 是 |
| `voiceprint/` | `@streamapp/voiceprint` | 容器 | `ghcr.io/jaggerh/voiceprint-server` | 是（仅 GPU） |
| `meituan/` | `@streamapp/meituan` | 代码（能力包：登录 / 领券 / 到店团购搜索 / 下单） | — | 否 |
| `whisper-asr/`、`whisperx-server/` | — | — | — | 仅存档，未接入 Stream |

## 目录约定

- `package.json#stream.backend` 是 Stream 读的**唯一契约**（image / port / health / gpu / env / volumes /
  mem / standby）。Stream 宿主拿它建容器、standby 管生灭；契约字段的定义见 Stream 仓库
  `docs/PACKAGE.md` 的容器槽位一节。
- `Dockerfile` 是镜像的**唯一来源**。workflow 里的镜像名从清单的 `image` 字段读，不另写一遍。
- 容器包的 npm 包只装 `package.json` + `README.md`（`files` 白名单）；Dockerfile / app.py 不进 npm 包，
  安装侧只认清单。
- 代码包（`package.json#stream.capability`，今天只有 `meituan/`）：`src/` 经 tsdown 打成**一个**
  `dist/index.js`，npm 包恰好 `package.json` + `README.md` + `dist/index.js`（Stream 安装门只放行这三个路径，
  `prepack` 挂的 `scripts/assert-npm-artifact.mjs` 守它）。它吃的宿主契约类型在 `sdk/capability/`
  （stream 仓库 `shared/capability/` 的拷贝，同步规则见 `sdk/README.md`）。每个代码包自带 `node_modules`
  （`npm ci`）、`npm run typecheck` / `npx vitest run src` / `npm run bundle`。
- 每个目录自带 README：接口、镜像 / 动词、在 Stream 里的字段说明。

## 发版

一个 tag 锁定整个目录：

```bash
# 先把 <dir>/package.json 的 version 改成目标版本；容器包还要把 stream.backend.image 的 tag 改成同一个版本
# （Stream 安装门拒 :latest——stream update 靠换 tag 让宿主重建容器）。workflow 两处都核，对不上就拒发。
git tag dewatermark-v1.0.1 && git push --tags
```

`.github/workflows/release.yml` 按 `package.json#stream.backend` 在不在分两条路：容器包构建并推
`ghcr.io/…:<版本>` + `:latest`；代码包 `npm ci && npm run bundle`（typecheck + 测试 + 产物闸）。
然后 `npm publish` 该目录（版本已在 npm 上则跳过，tag 可以重打）。PR / 非 tag push 只 build 不 push、不发。
**首发**人工做一次 `npm publish --access public`（scope 首个版本的 public 设置），之后交给 tag。
需要仓库 secret `NPM_TOKEN`。

## 用户怎么装

```bash
stream add @streamapp/dewatermark     # 或 ddddocr / mineru / voiceprint / meituan
```

前提：

- 容器包要 Stream 配置里 `manage_containers: true`（宿主要能建容器，本机要有 docker）；代码包不需要。
- GPU 包（mineru / voiceprint）要 nvidia container toolkit；voiceprint 没有 CPU 变体，无显卡的机器
  起不来。
- 装完后端重载即挂上；容器闲置到 `standby.idleMinutes` 自动回收，下次请求再唤醒。
