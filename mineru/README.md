# mineru-server

Thin FastAPI wrapper over [MinerU](https://github.com/opendatalab/MinerU) for Stream's
document-parsing package (`@streamapp/mineru`, see「在 Stream 里」below).

```
POST /parse   multipart: file=<image|pdf bytes>, type=<"pdf"|"image">  -> {markdown, json}
GET  /health                                                            -> {status, device, backend}
```

**Faithful by design.** Uses MinerU's `pipeline` backend: a born-digital PDF's text layer is
extracted directly (exact text/numbers, no fabrication), tables/formulas go through dedicated
models, and the VLM only handles scanned/image regions. There is deliberately no end-to-end
"re-read with a VLM" mode — that is the unfaithful path the feature exists to avoid. Critical
for investment-research PDFs where a fabricated number is catastrophic.

- CPU by default; auto-GPU when CUDA is present.
- Models download on first `/parse` to `/root/.cache` (persist with a volume).
- `MINERU_MODEL_SOURCE=modelscope` (CN-domestic) by default; set `huggingface` elsewhere.

## Build + push

Releases are built by the repo workflow: `git tag mineru-v<版本> && git push --tags` builds and
pushes `ghcr.io/jaggerh/mineru-server:<版本>` + `:latest`, then `npm publish` this directory.
Manual equivalent:

```bash
docker build -t ghcr.io/jaggerh/mineru-server:latest .
# isolated DOCKER_CONFIG to bypass the desktop credsStore, then push + make the package public
docker push ghcr.io/jaggerh/mineru-server:latest
```

## Smoke test

```bash
docker run --rm -p 8081:80 -v mineru-cache:/root/.cache ghcr.io/jaggerh/mineru-server:latest
curl -F file=@paper.pdf -F type=pdf http://127.0.0.1:8081/parse
```

## 在 Stream 里

Optional document/image → markdown backend (no credentials, no source). Install with
`stream add @streamapp/mineru`; the backend builds the container from `package.json#stream.backend`
and standby manages its lifecycle.

A service-only package: it contributes a backend container (reached through the backend at
`/_p/mineru`, or a remote relay when `mineru_url` is configured) but registers no source
adapter/normalizer. Self-built mirror of MinerU behind a thin FastAPI wrapper.

Local container = "free but slow" (CPU default, GPU when present); cloud relay (`mineru_url`) = the
paid fast lane. Both tiers run the SAME MinerU — cloud sells throughput, not a better model
(frontier VLMs are *less* faithful on documents than MinerU's pipeline). Output quality on the
2026-07-30 run (see `backend.gpu` below for the setup): Chinese text accurate, tables reconstructed
as HTML, nothing invented.

### backend

`gpu: true` —— MinerU's deterministic-first pipeline (layout → table-structure → formula → reading
order) runs best on a GPU; born-digital PDFs extract the text layer directly (no VLM rewrite → exact
numbers), the VLM only OCRs scanned/image regions.

Measured 2026-07-30 (RTX 4080 16GB, host 46GB RAM; input = one screenshot dense with Chinese text,
via `POST /_p/mineru/parse`). These are readings from that setup, not universal constants —
VRAM/RSS scale with input size and model backend:

- image on disk 9.64GB; model cache volume `stream_mineru-cache` 1.2GB
- idle RSS 335MiB (models load lazily — nothing resident until a request arrives)
- one-image parse: peak RSS 3.3GB (under a third of the `mem: 10G` cap)
- VRAM delta ~1.6GB (whole card 7066 → 8652 MiB)

With standby it costs 0 while unused.

- `env.MINERU_DEVICE` —— MinerU device autodetect; pin a backend/model here if needed.
- `volumes` —— persist the model + layout/table/formula weights so they don't re-download on restart.
- `mem: 10G` —— cap host RAM (model lives in VRAM; this bounds the python/pdf/image side). swap
  disabled (memswap = mem) so a runaway OOM-kills fast rather than host swap-thrash.
- `standby.startTimeoutSeconds: 120` —— MinerU's model load (layout/table/formula weights) is heavier
  than the other backends' cold start; give it more runway before the standby-manager gives up
  waiting for health.
