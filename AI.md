# AI Vision Analysis

A single endpoint, `POST /api/ai/analyze`, that sends an image to a vision model and returns a text description. Used by the static image viewer's "AI Analysis" button and by the report builder's "Visual / AI-assisted description" section.

## Endpoint

```
POST /api/ai/analyze
  { "image_url": "<url or data URL>", "file_id": "<optional asset id>" }
```

**No auth dependency.** Anyone with the URL can call it (the frontend protects it via `ProtectedRoute`, but the API itself is open). See the abuse-surface note at the bottom.

### Response shapes

```jsonc
// Synchronous success (most calls)
HTTP 200
{ "description": "SCENE: …\nQUALITY ISSUES: …\nSAFETY ISSUES: …", "cached": false }

// Cache hit (in-memory dict, cleared on restart, OR DB row with status=done)
HTTP 200
{ "description": "…", "cached": true }

// Background generation in progress, poll again
HTTP 202
{ "status": "generating", "message": "Analysis in progress, please wait" }

// Bad input, image not found, wrong media_type, invalid URL
HTTP 400 { "detail": "..." }

// Vision API failed (network, auth, model error)
HTTP 502 { "detail": "AI analysis failed: ..." }
```

The frontend polls `analyze` every 2 seconds (up to 30 attempts) while the response is 202.

## How `image_url` and `file_id` interact

The endpoint accepts either or both. `_resolve_vision_url` in `app/services/ai.py:53` figures out what to send to the vision API:

1. **`file_id` provided** → ignored if asset's `media_type != "image"` (400 "AI vision is only supported for image files"). Otherwise: read the bytes from MinIO, encode as `data:image/...;base64,...`, cache-key = `file:<id>`.
2. **`image_url` is a `data:image/...` URL** → passed through unchanged. Cache-key = first 256 chars.
3. **`image_url` is a public HTTPS URL** (not localhost, not an internal IP) → passed through. Cache-key = the URL.
4. **`image_url` is anything else** (presigned MinIO, localhost) → the backend fetches it server-side, then sends the bytes as a base64 data URL. Cache-key = `fetched:<url>`.

The local/private-IP detection is in `_host_is_private_or_local` (`ai.py:30`). It checks for `localhost`, `minio`, and any IPv4/IPv6 address that `ipaddress` reports as private, loopback, or link-local.

## Caching

There are **two** caches, layered:

### 1. Database cache (per `FileAsset`)

For requests with a `file_id`, the asset row stores:

| Column | Values |
|---|---|
| `ai_description` | The cached output text, or NULL |
| `ai_description_status` | `null` (never tried) / `"generating"` / `"done"` / `"failed"` |

`/analyze`:
- `status="done"` and `ai_description` set → returns cached, no model call
- `status="generating"` → returns 202, no model call (background task is already running)
- Otherwise → runs the vision call inline, writes the result to the asset row, sets `status="done"`. On failure: sets `status="failed"`.

### 2. In-memory cache (process-local)

`_cache: dict[str, str]` in `services/ai.py`. Keyed by `file:<id>` for asset calls, the data URL for inline data URLs, the URL for public URLs, or `fetched:<url>` for backend-fetched URLs.

Cleared on every process restart. Reduces duplicate API calls during a single session (e.g., same image opened in two viewers).

## Automatic generation on upload

`POST /api/upload/single` for an **image** fires `generate_and_cache_ai_description(asset.id)` as a FastAPI `BackgroundTask`. The asset is created immediately with `ai_description_status="generating"`; the background task overwrites `ai_description` + `status` when the vision API responds.

This means: by the time a user clicks "AI Analysis" in the viewer, the result is often already in the row.

## Prompt

The prompt is hardcoded in `app/services/ai.py:122`. It instructs the model to:

- Treat the image as a 360° panorama (curved lines = lens distortion, not architecture).
- Produce three sections: `SCENE:`, `QUALITY ISSUES:`, `SAFETY ISSUES:`.
- Write at least 3–5 sentences per section, no cross-section repetition.

If you change the prompt or the section structure, also update:
- `frontend-next/lib/engineeringReportPdf.ts` (consumes the `SCENE/QUALITY/SAFETY` headings as plain text).
- This file.

## Model configuration

| Variable | Default | Description |
|---|---|---|
| `VISION_API_URL` | `http://192.168.50.103:11434/v1/chat/completions` | OpenAI-compatible chat-completions endpoint |
| `VISION_API_KEY` | *(empty)* | Bearer token, omitted from headers if empty |
| `VISION_MODEL` | `qwen3-vl:8b` | Model name passed in the request body |

The default points at a local Ollama instance on the LAN. For Hyperbolic cloud, set:

```env
VISION_API_URL=https://api.hyperbolic.xyz/v1/chat/completions
VISION_API_KEY=<your-key>
VISION_MODEL=Qwen/Qwen2-VL-72B-Instruct  # or whatever model you've enabled
```

Any OpenAI-compatible chat-completions endpoint that accepts multimodal `image_url` content blocks will work.

### Thinking-model handling

For Qwen3-VL and similar "thinking" models that emit `<think>…</think>` blocks (or stash reasoning in a separate `message.thinking` field), the service:

1. Strips inline `<think>...</think>` blocks (keeps the visible text).
2. If the visible text is empty, falls back to `message.reasoning` / `message.thinking`.
3. Inside that fallback, looks for the last `SCENE:` heading and trims to that, the model embeds the structured report at the end of its reasoning chain.

If your model never emits `<think>` blocks (most non-thinking models), the fallback path is never reached.

## Request params

The body sent to the vision API:

```python
{
    "model": settings.vision_model,
    "messages": [{
        "role": "user",
        "content": [
            { "type": "text", "text": "<hardcoded prompt>" },
            { "type": "image_url", "image_url": { "url": "<resolved URL>" } }
        ]
    }],
    "max_tokens": 4096,
    "temperature": 0.3,
    "top_p": 0.9,
    "frequency_penalty": 1.3,
    "stream": False,
}
```

The high `frequency_penalty` discourages repetition across the three sections, Qwen3-VL otherwise tends to restate the same observation in QUALITY and SAFETY.

## Benchmark scripts

`backend/scripts/` contains several vision-model evaluation scripts:

```
benchmark_vision.py
benchmark_vision_bert.py
benchmark_vision_bert_oldP.py
benchmark_vision_bert_Updates.py
benchmark_vision_bert_Updates2.py
benchmark_vision_kimi.py
eval_set.json
```

These are **research artifacts**, not part of the application runtime. They were used to compare:

- Plain qualitative comparison (`benchmark_vision.py`)
- BERT-similarity-scored comparisons against a gold standard (`benchmark_vision_bert*.py`)
- The Kimi-VL model (`benchmark_vision_kimi.py`)

`eval_set.json` is the gold-standard dataset: a list of `{image_path, expected_description}` entries that the BERT scripts compare model output against.

If you are evaluating a new model:

1. Set `VISION_API_URL` / `VISION_API_KEY` / `VISION_MODEL` in your shell env.
2. Run the script most relevant to what you want to measure.
3. Output is printed to stdout; redirect to a file to save results.

These scripts are unmaintained, feel free to delete the variants you don't need, or move them to `scripts/benchmark/` for clarity. They are not imported anywhere in `app/`.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `502 AI analysis failed: HTTP 401` | Wrong / missing `VISION_API_KEY` | Set the key |
| `502 AI analysis failed: connect ECONNREFUSED` | Local Ollama not running, or wrong `VISION_API_URL` | Start Ollama or fix the URL |
| `200` but `description` is empty / generic | Image is too small / blurry / not actually construction | Check the image; consider lowering `max_tokens` or trying a different model |
| Endless 202 polling | Background task crashed without updating `ai_description_status` | Force-retry by `UPDATE file_assets SET ai_description_status='failed' WHERE id='…';` and re-call |
| Same description for two different images | In-memory cache hit on a stale key | Restart the backend, or pass `file_id` (which uses a per-asset cache key) |

## Abuse surface

`/api/ai/analyze` is unauthenticated. Anyone who hits the route can trigger a vision-model call, which may:

- Cost money (if you are paying per-token to a hosted provider).
- Pull bytes from MinIO via `file_id`.
- Cause the backend to fetch an arbitrary HTTP URL server-side (`image_url`-resolution path 4). Mitigated by `_host_is_private_or_local`, which blocks loopback/private IPs, but the backend will still fetch external HTTP URLs.

If you deploy this on the public internet, add `Depends(get_current_user)` to the route and reconsider whether you want unauthenticated public-URL fetching at all. Same fix as for the file-content endpoints, though those are intentionally public.

## Where the code lives

| Concern | File |
|---|---|
| Route | `app/api/ai.py` |
| Resolver + prompt + cache | `app/services/ai.py` |
| Background generation on upload | `app/services/ai.py::generate_and_cache_ai_description` |
| `ai_description` column on `FileAsset` | `app/models.py` |
| Schema migration that added the column | `services/db_migrations.py::ensure_file_assets_ai_description` |
| Benchmarks (research) | `backend/scripts/benchmark_vision_*` |
