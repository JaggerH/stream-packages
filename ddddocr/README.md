# ddddocr — 验证码 / 小块图片文字识别

给 Stream recipe 的 `call` 步骤当外部工具：recipe 走到需要认图的那一步（典型是登录页的图形验证码），
把那**一个元素的截图**送过来，拿回一串字符继续往下填。模型是 [ddddocr](https://github.com/sml2h3/ddddocr)，
权重打在 pip wheel 里，运行时零下载；CPU 一次约 20ms。

## 接口

```
POST /ocr     {"image": "<base64，可带 data:image/png;base64, 前缀>", "charset": "0123456789"?}  -> {"text": "..."}
GET  /health  -> {"status": "ok"}
```

- `charset` 可选：只在这几个字符里挑答案。默认模型是给字母数字混排调的，纯数字验证码会把 0 认成 o、
  9 认成 g——限住字符集就没这类错。限成哪一套是站点知识，由 recipe 在 `call.options` 里声明。
- 图片超过 `DDDDOCR_MAX_IMAGE_BYTES`（默认 512KB）回 413：`call` 只该送一个元素的截图，这么大多半是
  选择器选到了整页。

## 镜像

`ghcr.io/jaggerh/ddddocr-server`，CPU-only（GPU 对几 MB 的模型没有收益，`gpu: true` 只会让没装
nvidia toolkit 的机器起不来）。构建期就加载一次模型，"装上了"≠"加载得起来"。

## 在 Stream 里

```bash
stream add @streamapp/ddddocr        # 装清单；容器由宿主在 manage_containers: true 时建出来
```

recipe 侧只点名 `service: "ddddocr"`（`call` 步骤给不了 URL，宿主按已装包解析到 `/_p/ddddocr`），
并在 `meta.effects` 里申报 `call`。用它的内置 recipe：`eastmoney-login`。
