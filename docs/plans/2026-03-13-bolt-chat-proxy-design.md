# Bolt Chat Proxy Design (OpenAI-Compatible)

Date: 2026-03-13

## Summary
Build a local FastAPI adapter that exposes OpenAI-compatible `chat/completions` on `127.0.0.1:8217` and maps requests to `https://bolt.new/api/chat/v2`, including SSE streaming. Provide a `GET /v1/models` endpoint for client discovery. Enforce an allowlist of supported models. Use minimal upstream auth only if required.

## Goals
- Provide OpenAI-compatible `POST /v1/chat/completions` for local clients.
- Support `stream: true` (SSE) and non-streaming responses.
- Support tools/function_call.
- Enforce a strict model allowlist.
- Support optional local API key validation.
- Keep upstream auth minimal and user-supplied only if required.

## Non-Goals
- Changing upstream authentication or credentials.
- Guaranteeing backend permissions beyond what the upstream account allows.
- Implementing a full `/v1/responses` API.

## Architecture (Design A)
- Service: FastAPI
- Port: `127.0.0.1:8217`
- Endpoints:
  - `POST /v1/chat/completions`
  - `GET /v1/models`
- Upstream: `https://bolt.new/api/chat/v2`
- HTTP client: `httpx`
- Python: 3.12 via `uv`

## Authentication (Design B)
### Local OpenAI Auth
- Accept `Authorization: Bearer sk-op` by default.
- Override via env `LOCAL_OPENAI_API_KEY`.
- If env is empty, skip local auth checks.

### Upstream Auth (Minimal)
- Only request and send the minimal required cookie/header if upstream demands it.
- No credentials are stored in code; provided via env or runtime configuration.

## Model Handling (Design C)
Allowlist only:
- `claude-haiku-4-5-20251001`
- `claude-sonnet-4-5-20250929`
- `claude-opus-4-5-20251101`
- `claude-opus-4-6`
- `claude-sonnet-4-6`

Unknown models return HTTP 400 with an OpenAI-style error payload.

## Request/Response Mapping (Design D)
- Input: OpenAI `chat.completions` request schema.
- Translate into `/api/chat/v2` request schema (captured via jsr-reverse).
- Output:
  - If `stream: true`, return SSE in OpenAI chunk format.
  - Else, return standard `chat.completion` JSON.
- Tools/function_call are passed through and mapped to upstream equivalents.

## Error Handling (Design E)
- Upstream 401/403 -> OpenAI error payload, status passthrough.
- Schema mismatch / unexpected payload -> HTTP 502 with minimal diagnostic hint.
- Network errors -> HTTP 502.

## Configuration
- `LOCAL_OPENAI_API_KEY` (optional)
- `UPSTREAM_AUTH_*` (placeholder names; to be finalized after locate phase)

## Testing
- Manual curl test for non-streaming.
- Manual SSE test for streaming.
- Model allowlist rejection test.
- Auth required/optional checks.

## Next Step
Create an implementation plan and then implement the adapter.
