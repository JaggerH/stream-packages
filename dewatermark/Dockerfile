# 去水印后端（`packages/dewatermark`）：一张图进、一张图出。找角落里的平台水印标（模板匹配），
# 用 LaMa 把那一块补掉。**CPU-only by design**——LaMa 在 CPU 上补一个 2048² 图角落的小块约 3–6s，
# 这条链路一次 4 张、上游生图本身就要 20s，GPU 省下的几秒换不来 `gpu: true` 在 CPU 主机上起不来的代价。
#
# 模型烤进镜像（`iopaint download`），运行期不下任何东西——容器里拉 HF 走的是不稳链路
# （authoring-backend-image.md 第一条）。
FROM python:3.11-slim

WORKDIR /app

# iopaint 拉的是非 headless 的 opencv-python，import 时要 libGL / libglib；slim 基底没有。
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

# 贵层在前：torch(cpu) + iopaint；改 app.py 不该重装它们。
COPY requirements.txt .
RUN pip install --no-cache-dir --retries 10 --timeout 120 \
      -i https://pypi.tuna.tsinghua.edu.cn/simple \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      -r requirements.txt

# LaMa 权重（约 200MB）烤进镜像；构建期加载一次——装上了不等于加载得起来。
RUN iopaint download --model lama && \
    python -c "from iopaint.model_manager import ModelManager; ModelManager(name='lama', device='cpu'); print('lama ok')"

# 水印模板：从真实样张裁的「AI 生成」标（见 templates/README）。
COPY templates/ templates/
COPY app.py .

EXPOSE 80
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "80"]
