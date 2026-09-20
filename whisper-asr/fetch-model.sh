#!/usr/bin/env bash
# Download the baked default ASR model (faster-whisper large-v3, CT2 format) into the build
# context before `docker build`. Files land in ./models/large-v3/ which the Dockerfile COPYs.
#
# Source = ModelScope (阿里魔搭) not HuggingFace, for two reasons discovered the hard way:
#   1. HF / hf-mirror downloads of the ~3GB model.bin repeatedly truncate through the proxy
#      (SSL EOF); ModelScope is a domestic CDN — direct, fast (~28MB/s), reliable.
#   2. CTranslate2 4.x needs `vocabulary.json` in the model dir (NOT vocabulary.txt); Systran's
#      HF repo doesn't ship it as a standalone file, but the ModelScope mirror does. Loading the
#      model without it fails with "Cannot load the vocabulary from the model directory".
set -euo pipefail
cd "$(dirname "$0")"
DIR=models/large-v3
mkdir -p "$DIR"
MS="https://modelscope.cn/api/v1/models/pengzhendong/faster-whisper-large-v3/repo?Revision=master&FilePath="
# vocabulary.json is required by CTranslate2; vocabulary.txt is NOT used by large-v3.
for f in config.json model.bin tokenizer.json vocabulary.json preprocessor_config.json; do
  echo "downloading $f ..."
  curl -fL --retry 8 --retry-delay 2 -C - --max-time 1800 -o "$DIR/$f" "${MS}${f}"
done
echo "model ready in $DIR ($(du -sh "$DIR" | cut -f1))"
