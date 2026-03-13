# Bolt Chat Proxy (OpenAI-Compatible) Design

Date: 2026-03-13

## Summary
Build a local Python 3.12 proxy that exposes OpenAI-compatible `/v1/chat/completions` (plus `/v1/models`) and translates requests to Bolt's `/api/chat/v2`. Streaming SSE is supported. Local auth defaults to `sk-op` and is configurable. Upstream auth uses minimal required fields only when necessary.

## Goals
- Provide OpenAI-compatible chat endpoint at `127.0.0.1:8217`.
- Support streaming (`stream: true`) and non-streaming responses.
- Enforce model allowlist matching Bolt-supported models.
- Minimize required auth fields; prompt only if upstream requires.
- Work with `uv` on Python 3.12.

## Non-Goals
- Bypass upstream authorization or billing.
- Emulate OpenAI function/tool behavior beyond schema mapping.
- Persist auth secrets in code.

## Architecture
- **Service**: FastAPI
- **HTTP client**: httpx (supports streaming)
- **Endpoints**:
  - `POST /v1/chat/completions`
  - `GET /v1/models`
- **Upstream**: `https://bolt.new/api/chat/v2`

## Model Handling
Allowed models:
- `claude-haiku-4-5-20251001`
- `claude-sonnet-4-5-20250929`
- `claude-opus-4-5-20251101`
- `claude-opus-4-6`
- `claude-sonnet-4-6`

Behavior:
- If `model` not in allowlist: return `400` with OpenAI-style error.

## Authentication
### Local OpenAI-compatible auth
- Default accepted key: `sk-op`.
- Override via env: `LOCAL_OPENAI_API_KEY`.
- If env is empty/unset, skip local auth.

### Upstream auth
- Only include minimal required fields (cookie or bearer).
- If upstream returns 401/403, respond with OpenAI error shape and prompt user to supply the minimal required auth field(s).

## Request/Response Mapping
- Input: OpenAI `chat.completions` request.
- Output: OpenAI `chat.completions` response.
- The adapter translates to Bolt `/api/chat/v2` schema based on captured live request/response.
- Tools/functions are mapped to Bolt schema when present.

## Streaming
- If `stream: true`, proxy transforms upstream stream to OpenAI SSE:
  - `data: {"id":...,"object":"chat.completion.chunk",...}`
  - `data: [DONE]`
- If upstream is non-SSE, convert to SSE chunks in the adapter.

## Error Handling
- Upstream 401/403 ¡ú OpenAI error JSON with `type`, `code`.
- Upstream schema mismatch ¡ú 502 with concise hint.
- Validation errors (missing model, malformed messages) ¡ú 400.

## Configuration
- `LOCAL_OPENAI_API_KEY` (optional)
- `UPSTREAM_COOKIE` (optional)
- `UPSTREAM_BEARER` (optional)
- `BOLT_BASE_URL` default `https://bolt.new`

## Logging
- Request ID per incoming request.
- Log upstream status, elapsed time, and conversion failures.
- Do not log raw credentials.

## Testing
- Smoke test:
  - `GET /v1/models` returns allowlist.
  - `POST /v1/chat/completions` returns completion for a trivial prompt.
  - Streaming request returns SSE chunks and `[DONE]`.

## Open Questions
- Exact `/api/chat/v2` request/response schema and streaming format (to be captured via jsr-reverse before implementation).
