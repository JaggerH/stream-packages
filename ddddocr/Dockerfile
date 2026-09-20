# OCR backend for the `call` recipe step. **CPU-only by design** — ddddocr's model is a few MB
# and a call is ~20ms on CPU; a GPU reservation here would be `gpu: true` on a service that cannot
# use it, which is exactly the failure the authoring guide warns about (compose then refuses to
# start on a CPU-only host for no benefit at all).
#
# No fetch-model.sh, no COPY models/: ddddocr ships its ONNX weights inside the pip wheel. Nothing
# is downloaded at runtime — which is the property the "bake the model in" rule exists to protect.
FROM python:3.11-slim

WORKDIR /app

# 依赖层在前、app 代码在后：改 app.py 不该重装一遍 onnxruntime（贵层在前、volatile 在后）。
COPY requirements.txt .
# 国内 pip 源：国际线路过代理拉 onnxruntime 这种几十 MB 的 wheel 反复截断（和 whisper-asr /
# voiceprint 同一个坑，同一个解法）。重试给足当保险。
RUN pip install --no-cache-dir --retries 10 --timeout 120 \
      -i https://pypi.tuna.tsinghua.edu.cn/simple \
      -r requirements.txt

# 构建期就把模型加载一次：ddddocr 的权重虽然打在 wheel 里，但"装上了"不等于"加载得起来"
# （numpy/onnxruntime 的 ABI 不合会在 import 期才炸）。在这里炸 = build 失败，看得见；
# 留到运行期炸 = 容器起来了、健康检查绿着，第一次真用才发现。
RUN python -c "import ddddocr; ddddocr.DdddOcr(show_ad=False, beta=False); print('ddddocr ok')"

COPY app.py .

EXPOSE 80
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "80"]
