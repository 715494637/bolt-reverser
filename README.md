# bolt-reverser

Tooling for experimenting with Bolt/StackBlitz chat flows and a local OpenAI-compatible proxy for your own accounts.

## Compliance
- Use only with accounts you own or are authorized to test.
- Follow the target service terms and policies.
- Do not bypass paywalls, WAF/anti-bot protections, or access controls.

## Contents
- `bolt_openai_proxy.py` FastAPI proxy exposing `/v1/models` and `/v1/chat/completions`.
- `refresh_bolt_sessions.py` Refreshes session state for accounts in `bolt_accounts.json`.
- `e2e_register_stackblitz.py` Playwright-assisted registration flow with manual Turnstile.
- `e2e_register_stackblitz_pydoll.py` pydoll-based automation variant (authorized testing only).
- `imap_2925.py` IMAP helper to extract confirmation links.

## Requirements
- Python 3.10+
- Recommended: `uv` for running PEP 723 scripts
- Playwright browser install: `playwright install chromium`

## Setup
1. Create `.env` (ignored by git) and set optional proxy auth.
   - `BOLT_PROXY_KEY` Optional. If set, requests must include `Authorization: Bearer <key>`.
   - Other tuning options are documented at the top of `bolt_openai_proxy.py`.
2. Create `bolt_accounts.json` (ignored by git).
   - Example:

```json
{
  "rotation": "round_robin",
  "accounts": [
    {
      "name": "account-1",
      "email": "you@example.com",
      "password": "YOUR_PASSWORD",
      "cookie": "__session=YOUR_SESSION_COOKIE",
      "project_id": "",
      "default_model": "claude-sonnet-4-6",
      "max_concurrency": 1
    }
  ]
}
```

## Run the proxy
```bash
uv run bolt_openai_proxy.py
```

Default listen address is `0.0.0.0:8000`.

Example requests:
```bash
curl http://localhost:8000/v1/models

curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $BOLT_PROXY_KEY" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"hello"}]}'
```

## Refresh account sessions
```bash
uv run refresh_bolt_sessions.py --all
```

## Registration helpers
Playwright (manual Turnstile):
```bash
python e2e_register_stackblitz.py --email you@example.com --password "..." --submit
```

pydoll variant (authorized testing only):
```bash
uv run e2e_register_stackblitz_pydoll.py --email you@example.com --password "..."
```

## Notes on secrets
- Do not commit real cookies, passwords, or tokens.
- Keep `.env` and `bolt_accounts.json` local only.
- Override any default credentials in `imap_2925.py` via CLI flags.
