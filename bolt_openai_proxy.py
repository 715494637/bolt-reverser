# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastapi",
#   "uvicorn",
#   "httpx",
# ]
# ///

import asyncio
import json
import math
import os
import re
import secrets
import shlex
import threading
import time
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

ACCOUNTS_PATH = Path("bolt_accounts.json")
BOLT_ENV_PATH = Path(".env")
BOLT_STATE_DEFAULT = ACCOUNTS_PATH
BOLT_BASE = "https://bolt.new"
CHAT_ENDPOINT = f"{BOLT_BASE}/api/chat/v2"
TEMPLATE_ENDPOINT = f"{BOLT_BASE}/api/template"
CHATS_ENDPOINT = f"{BOLT_BASE}/api/chats"
TOKEN_ENDPOINT = f"{BOLT_BASE}/api/token"

MODEL_LIST = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5-20250929",
    "claude-opus-4-5-20251101",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        # Ignore .env parsing errors; fallback to process env
        return


load_dotenv(BOLT_ENV_PATH)
AUTH_KEY = os.getenv("BOLT_PROXY_KEY", "").strip()
QUOTA_REFRESH_SECONDS = int(os.getenv("BOLT_QUOTA_REFRESH_SECONDS", "300"))
QUOTA_COOLDOWN_SECONDS = int(os.getenv("BOLT_QUOTA_COOLDOWN_SECONDS", "60"))
QUOTA_STATE_PATH = Path(os.getenv("BOLT_QUOTA_STATE_PATH", str(BOLT_STATE_DEFAULT)))
ENABLE_REASONING = os.getenv("BOLT_ENABLE_REASONING", "0").strip() in ("1", "true", "True")
PROJECT_MODE = os.getenv("BOLT_PROJECT_MODE", "reuse").strip().lower()
PROJECT_PREFETCH = int(os.getenv("BOLT_PROJECT_PREFETCH", "3"))
DEBUG_STREAM = os.getenv("BOLT_DEBUG_STREAM", "0").strip() in ("1", "true", "True")
BOLT_DEBUG_MODE = os.getenv("BOLT_DEBUG_MODE", "0").strip() in ("1", "true", "True")
BOLT_STDOUT_LOG_MODE = os.getenv("BOLT_STDOUT_LOG_MODE", "brief").strip().lower() or "brief"
BOLT_DEBUG_LOG_PATH = Path(os.getenv("BOLT_DEBUG_LOG_PATH", "logs/bolt_proxy_debug.jsonl"))
BOLT_DEBUG_BODY_LIMIT = int(os.getenv("BOLT_DEBUG_BODY_LIMIT", "50000"))
BOLT_SESSION_POLL_SECONDS = int(os.getenv("BOLT_SESSION_POLL_SECONDS", "60"))
AUTH_COOLDOWN_SECONDS = int(os.getenv("BOLT_AUTH_COOLDOWN_SECONDS", "3600"))
AUTH_REFRESH_ON_FAIL = os.getenv("BOLT_AUTH_REFRESH_ON_FAIL", "1").strip() in ("1", "true", "True")
AUTH_REFRESH_CONCURRENCY = int(os.getenv("BOLT_AUTH_REFRESH_CONCURRENCY", "2"))
AUTH_REFRESH_REQUEST_TIMEOUT = int(
    os.getenv(
        "BOLT_AUTH_REFRESH_REQUEST_TIMEOUT",
        os.getenv("BOLT_REFRESH_REQUEST_TIMEOUT", os.getenv("BOLT_REFRESH_OAUTH_TIMEOUT", "30")),
    )
)
AUTH_REFRESH_COMMAND = os.getenv("BOLT_AUTH_REFRESH_COMMAND", "uv run refresh_bolt_sessions.py").strip()
BOLT_USER_AGENT = os.getenv(
    "BOLT_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
)

USE_PROJECT_POOL = PROJECT_MODE in {"new", "per_request", "new_each_request", "pool", "random"}
USE_PROJECT_FORK = PROJECT_MODE in {"fork"}
ALWAYS_NEW_PROJECT = USE_PROJECT_POOL or USE_PROJECT_FORK

if BOLT_STDOUT_LOG_MODE not in {"brief", "full", "off"}:
    BOLT_STDOUT_LOG_MODE = "brief"

if PROJECT_MODE not in {"reuse", "new", "per_request", "new_each_request", "fork", "pool", "random"}:
    PROJECT_MODE = "reuse"
if PROJECT_PREFETCH < 0:
    PROJECT_PREFETCH = 0
if AUTH_REFRESH_CONCURRENCY < 1:
    AUTH_REFRESH_CONCURRENCY = 1

_DEBUG_LOG_LOCK = threading.Lock()
_ACCOUNTS_MTIME = 0.0
_ACCOUNTS_RELOAD_LOCK = asyncio.Lock()
_SESSION_PROJECTS: Dict[str, str] = {}
_SESSION_LOCK = asyncio.Lock()
_AUTH_REFRESH_TASKS: Dict[str, asyncio.Task] = {}
_AUTH_REFRESH_TASKS_LOCK = asyncio.Lock()
_AUTH_REFRESH_SEM = asyncio.Semaphore(AUTH_REFRESH_CONCURRENCY)


def _clip_debug_text(value: str) -> str:
    if not isinstance(value, str):
        return str(value)
    if BOLT_DEBUG_BODY_LIMIT > 0 and len(value) > BOLT_DEBUG_BODY_LIMIT:
        omitted = len(value) - BOLT_DEBUG_BODY_LIMIT
        return f"{value[:BOLT_DEBUG_BODY_LIMIT]}\n...<truncated {omitted} chars>"
    return value


def _mask_secret(value: str) -> str:
    if not isinstance(value, str):
        return str(value)
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:6]}...{value[-4:]} (len={len(value)})"


def _sanitize_for_debug(value: Any, path: Tuple[str, ...] = ()) -> Any:
    secret_keys = {
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "x-api-key",
        "api-key",
    }
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in secret_keys:
                if isinstance(item, list):
                    sanitized[key] = [_mask_secret(str(v)) for v in item]
                else:
                    sanitized[key] = _mask_secret(str(item))
                continue
            sanitized[key] = _sanitize_for_debug(item, path + (str(key),))
        return sanitized
    if isinstance(value, list):
        return [_sanitize_for_debug(item, path + ("[]",)) for item in value]
    if isinstance(value, str):
        return _clip_debug_text(value)
    return value


def _write_debug_record(level: str, event: str, data: Optional[Dict[str, Any]] = None) -> None:
    if not BOLT_DEBUG_MODE:
        return
    try:
        BOLT_DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now().astimezone().isoformat(),
            "level": level,
            "event": event,
            "data": _sanitize_for_debug(data or {}),
        }
        with _DEBUG_LOG_LOCK:
            with BOLT_DEBUG_LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        # Never let debug logging affect proxy behavior.
        return


def _stdout_enabled(level: str) -> bool:
    if BOLT_STDOUT_LOG_MODE == "off":
        return False
    if level == "debug":
        return BOLT_DEBUG_MODE and BOLT_STDOUT_LOG_MODE == "full"
    return True


def _log(level: str, msg: str, *, event: Optional[str] = None, data: Optional[Dict[str, Any]] = None) -> None:
    if _stdout_enabled(level):
        print(f"[proxy][{level}] {msg}")
    if BOLT_DEBUG_MODE:
        payload = {"message": msg}
        if data:
            payload.update(data)
        _write_debug_record(level, event or "log", payload)


def log_info(msg: str) -> None:
    _log("info", msg)


def log_warn(msg: str) -> None:
    _log("warn", msg)


def log_error(msg: str) -> None:
    _log("error", msg)


def log_debug(msg: str, event: str, data: Optional[Dict[str, Any]] = None) -> None:
    _log("debug", msg, event=event, data=data)


BOLT_PREFIX_HEX = "626f6c742d63632d6167656e74"
BOLT_PREFIX_TEXT = "bolt-cc-agent"


def strip_bolt_prefix(text: str) -> str:
    if not text:
        return text
    if text.startswith(BOLT_PREFIX_HEX):
        text = text[len(BOLT_PREFIX_HEX):]
    if text.startswith(BOLT_PREFIX_TEXT):
        text = text[len(BOLT_PREFIX_TEXT):]
    return text


TOOL_CALL_PREFIX = "[tool_call]"
TOOL_CALL_RE = re.compile(r"\[tool_call\]\s*([A-Za-z0-9_.:-]+)\s*\((.*)\)\s*$")


def extract_tool_call_from_text(text: str) -> Optional[Dict[str, str]]:
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("`").strip()
        match = TOOL_CALL_RE.search(line)
        if not match:
            continue
        name = match.group(1).strip()
        args_raw = match.group(2).strip()
        if not args_raw:
            return {"name": name, "arguments": "{}"}
        # Validate JSON; if invalid, return None so we keep plain text
        try:
            json.loads(args_raw)
        except Exception:
            return None
        return {"name": name, "arguments": args_raw}
    return None


def is_tool_call_candidate(text: str) -> bool:
    stripped = text.lstrip().lstrip("`")
    if not stripped:
        return True
    return TOOL_CALL_PREFIX.startswith(stripped) or stripped.startswith(TOOL_CALL_PREFIX)


def _now_ts() -> float:
    return time.time()


def _parse_iso_ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def _iso_from_ts(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        ts_val = float(ts)
    except Exception:
        return None
    if ts_val <= 0 or not math.isfinite(ts_val):
        return None
    try:
        return datetime.fromtimestamp(ts_val).astimezone().isoformat()
    except Exception:
        return None


def _end_of_day_ts() -> float:
    now = datetime.now().astimezone()
    eod = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return eod.timestamp()


def _generate_project_id() -> str:
    return str(secrets.randbelow(90000000) + 10000000)


def _normalize_session_id(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        text = str(value).strip()
    except Exception:
        return ""
    if len(text) > 200:
        text = text[:200]
    return text


async def get_session_project_id(session_id: str, reset: bool = False) -> Optional[str]:
    if not session_id or not USE_PROJECT_POOL:
        return None
    async with _SESSION_LOCK:
        if reset or session_id not in _SESSION_PROJECTS:
            _SESSION_PROJECTS[session_id] = _generate_project_id()
        return _SESSION_PROJECTS[session_id]


class Account:
    def __init__(self, raw: Dict[str, Any]):
        self.name = raw.get("name") or "account"
        self.email = raw.get("email") or ""
        self.password = raw.get("password") or ""
        self.cookie = raw.get("cookie") or ""
        self.project_id = raw.get("project_id") or ""
        self.sender = raw.get("sender") or {}
        self.bolt_client_revision = raw.get("bolt_client_revision") or "f05eb54"
        self.default_model = raw.get("default_model") or MODEL_LIST[1]
        self.max_concurrency = int(raw.get("max_concurrency") or 0)
        self.framework = raw.get("framework") or "vite-react"
        self.prompt_mode = raw.get("prompt_mode") or "build"
        self.project_prompt = raw.get("project_prompt") or ""
        self.global_system_prompt = raw.get("global_system_prompt") or ""
        self.use_template_files = bool(raw.get("use_template_files", True))
        self.cache_template_files = bool(raw.get("cache_template_files", True))
        self.project_files_override = raw.get("project_files")
        self._in_flight = 0
        self._inflight_lock = asyncio.Lock()
        self._project_seen = set()
        self._project_pool: List[str] = []
        self._project_pool_lock = asyncio.Lock()
        self._cached_template_files = None
        self._bad_until = 0.0
        self._quota_lock = asyncio.Lock()
        self.quota_last_check = 0.0
        self.quota_daily_disabled_until = 0.0
        self.quota_monthly_exhausted = False
        self.quota_cooldown_until = 0.0
        self.quota_stats: Optional[Dict[str, Any]] = None
        self._load_quota(raw.get("quota") or {})

    def _load_quota(self, quota: Dict[str, Any]) -> None:
        self.quota_last_check = _parse_iso_ts(quota.get("last_check_at")) or 0.0
        self.quota_daily_disabled_until = _parse_iso_ts(quota.get("daily_disabled_until")) or 0.0
        self.quota_monthly_exhausted = bool(quota.get("monthly_exhausted", False))
        self.quota_cooldown_until = _parse_iso_ts(quota.get("cooldown_until")) or 0.0
        self.quota_stats = quota.get("last_stats")
        self._bad_until = _parse_iso_ts(quota.get("auth_disabled_until")) or self._bad_until
        if USE_PROJECT_POOL and PROJECT_PREFETCH:
            self._seed_project_pool(PROJECT_PREFETCH)

    def _seed_project_pool(self, target: int) -> None:
        while len(self._project_pool) < target:
            self._project_pool.append(_generate_project_id())

    async def acquire_project_id(self) -> str:
        if not USE_PROJECT_POOL:
            return self.project_id
        async with self._project_pool_lock:
            if self._project_pool:
                project_id = self._project_pool.pop(0)
            else:
                project_id = _generate_project_id()
            if PROJECT_PREFETCH:
                self._seed_project_pool(PROJECT_PREFETCH)
            return project_id

    async def try_acquire(self) -> bool:
        async with self._inflight_lock:
            now = _now_ts()
            if self.quota_monthly_exhausted:
                return False
            if self.quota_daily_disabled_until and now < self.quota_daily_disabled_until:
                return False
            if self.quota_daily_disabled_until and now >= self.quota_daily_disabled_until:
                self.quota_daily_disabled_until = 0.0
            if self.quota_cooldown_until and now < self.quota_cooldown_until:
                return False
            if self.quota_cooldown_until and now >= self.quota_cooldown_until:
                self.quota_cooldown_until = 0.0
            if self._bad_until and time.time() < self._bad_until:
                return False
            if self.max_concurrency <= 0:
                return True
            if self._in_flight < self.max_concurrency:
                self._in_flight += 1
                return True
            return False

    async def release(self):
        async with self._inflight_lock:
            if self.max_concurrency > 0 and self._in_flight > 0:
                self._in_flight -= 1

    def mark_bad(self, seconds: int = AUTH_COOLDOWN_SECONDS):
        # Temporarily disable an account that failed auth.
        self._bad_until = max(self._bad_until, time.time() + seconds)


async def schedule_auth_refresh(account: Account, reason: str = "") -> None:
    if not AUTH_REFRESH_ON_FAIL:
        return
    if not account.email or not account.password:
        log_warn(f"auth refresh skipped (missing email/password): {account.name}")
        return
    async with _AUTH_REFRESH_TASKS_LOCK:
        existing = _AUTH_REFRESH_TASKS.get(account.name)
        if existing and not existing.done():
            return
        task = asyncio.create_task(_auth_refresh_worker(account, reason))
        _AUTH_REFRESH_TASKS[account.name] = task


async def _auth_refresh_worker(account: Account, reason: str) -> None:
    async with _AUTH_REFRESH_SEM:
        try:
            log_info(f"auth refresh start | account={account.name} reason={reason or 'auth-fail'}")
            args = [
                *shlex.split(AUTH_REFRESH_COMMAND),
                "--account",
                account.name,
                "--request-timeout",
                str(max(5, AUTH_REFRESH_REQUEST_TIMEOUT)),
            ]
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                log_info(f"auth refresh ok | account={account.name}")
            else:
                out = (stdout or b"").decode(errors="replace").strip()
                err = (stderr or b"").decode(errors="replace").strip()
                detail = out or err or f"exit={proc.returncode}"
                log_warn(f"auth refresh failed | account={account.name} | {detail}")
        except Exception as exc:
            log_warn(f"auth refresh error | account={account.name} | {repr(exc)}")
        finally:
            try:
                await reload_accounts_if_changed()
            except Exception:
                pass
            async with _AUTH_REFRESH_TASKS_LOCK:
                _AUTH_REFRESH_TASKS.pop(account.name, None)


class AccountPool:
    def __init__(self, accounts: List[Account]):
        if not accounts:
            raise RuntimeError("No accounts loaded. Fill bolt_accounts.json")
        self.accounts = accounts
        self._idx = 0
        self._lock = asyncio.Lock()

    async def acquire_account(self, prefer_project_id: bool = False) -> Account:
        while True:
            async with self._lock:
                if not self.accounts:
                    raise RuntimeError("No accounts available")
                start = self._idx % len(self.accounts)
                order = (
                    [(idx, self.accounts[idx]) for idx in range(start, len(self.accounts))]
                    + [(idx, self.accounts[idx]) for idx in range(0, start)]
                )
            if prefer_project_id:
                order.sort(key=lambda item: 0 if item[1].project_id else 1)
            for idx, acct in order:
                if await acct.try_acquire():
                    async with self._lock:
                        if self.accounts:
                            self._idx = (idx + 1) % len(self.accounts)
                    return acct
            await asyncio.sleep(0.05)

    async def remove_account(self, acct: Account, reason: str = "") -> None:
        async with self._lock:
            if acct in self.accounts:
                self.accounts.remove(acct)
        # Persist removal to bolt_accounts.json
        try:
            if ACCOUNTS_PATH.exists():
                data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
                accounts = data.get("accounts", [])
                filtered = [a for a in accounts if (a.get("name") or "account") != acct.name]
                if len(filtered) != len(accounts):
                    backup = ACCOUNTS_PATH.with_suffix(".bak")
                    if not backup.exists():
                        backup.write_text(ACCOUNTS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
                    data["accounts"] = filtered
                    ACCOUNTS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            # Best-effort persistence
            pass


class AccountAuthError(Exception):
    pass


class AccountNoProjectError(Exception):
    pass


class AccountRateLimitError(Exception):
    pass


class QuotaManager:
    def __init__(self, state_path: Path, refresh_seconds: int, cooldown_seconds: int):
        self.state_path = state_path
        self.refresh_seconds = max(0, refresh_seconds)
        self.cooldown_seconds = max(0, cooldown_seconds)
        self._state_lock = asyncio.Lock()

    async def persist_state(self, accounts: List[Account]) -> None:
        async with self._state_lock:
            if not self.state_path.exists():
                return
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                return
            acc_map = {a.name: a for a in accounts}
            raw_accounts = data.get("accounts", [])
            for raw in raw_accounts:
                name = raw.get("name") or "account"
                acct = acc_map.get(name)
                if not acct:
                    continue
                raw["quota"] = {
                    "last_check_at": _iso_from_ts(acct.quota_last_check),
                    "daily_disabled_until": _iso_from_ts(acct.quota_daily_disabled_until),
                    "monthly_exhausted": bool(acct.quota_monthly_exhausted),
                    "cooldown_until": _iso_from_ts(acct.quota_cooldown_until),
                    "auth_disabled_until": _iso_from_ts(acct._bad_until),
                    "last_stats": acct.quota_stats,
                }
            data["accounts"] = raw_accounts
            self.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def _probe_account(
        self,
        client: httpx.AsyncClient,
        acct: Account,
        *,
        force: bool = False,
        ignore_blocks: bool = False,
        source: str = "request",
    ) -> str:
        now = _now_ts()
        if acct.quota_monthly_exhausted and not ignore_blocks:
            return "monthly_exhausted"
        if acct._bad_until and now < acct._bad_until and not ignore_blocks:
            return "auth"
        if acct.quota_daily_disabled_until and now < acct.quota_daily_disabled_until and not ignore_blocks:
            return "daily_disabled"
        if acct.quota_cooldown_until and now < acct.quota_cooldown_until and not ignore_blocks:
            return "cooldown"
        if self.refresh_seconds <= 0 and not force:
            return "ok"
        if not force and acct.quota_last_check and (now - acct.quota_last_check) < self.refresh_seconds:
            return "ok"

        async with acct._quota_lock:
            now = _now_ts()
            if acct.quota_monthly_exhausted and not ignore_blocks:
                return "monthly_exhausted"
            if acct._bad_until and now < acct._bad_until and not ignore_blocks:
                return "auth"
            if acct.quota_daily_disabled_until and now < acct.quota_daily_disabled_until and not ignore_blocks:
                return "daily_disabled"
            if acct.quota_cooldown_until and now < acct.quota_cooldown_until and not ignore_blocks:
                return "cooldown"
            if not force and acct.quota_last_check and (now - acct.quota_last_check) < self.refresh_seconds:
                return "ok"

            headers = bolt_headers(acct, project_id="")
            try:
                resp = await client.get(f"{BOLT_BASE}/api/token-stats", headers=headers)
            except Exception:
                return "error"
            if resp.status_code in (401, 403):
                acct.mark_bad()
                acct.quota_cooldown_until = max(acct.quota_cooldown_until, _now_ts() + AUTH_COOLDOWN_SECONDS)
                body = resp.text if hasattr(resp, "text") else ""
                if body:
                    log_warn(f"token-stats 401/403 | source={source} | ??={acct.name} | body={body}")
                await schedule_auth_refresh(acct, reason=f"token-stats:{source}")
                return "auth"
            if resp.status_code == 429:
                acct.quota_cooldown_until = _now_ts() + self.cooldown_seconds
                body = resp.text if hasattr(resp, "text") else ""
                if body:
                    log_warn(f"token-stats 429 | source={source} | ??={acct.name} | body={body}")
                return "cooldown"
            if resp.status_code != 200:
                body = resp.text if hasattr(resp, "text") else ""
                if body:
                    log_warn(f"token-stats {resp.status_code} | source={source} | ??={acct.name} | body={body}")
                return "error"
            try:
                payload = resp.json()
            except Exception:
                return "error"
            stats = payload.get("tokenStats") if isinstance(payload, dict) else None
            if not isinstance(stats, dict):
                return "error"
            acct.quota_last_check = _now_ts()
            acct.quota_stats = stats

            max_per_day = stats.get("maxPerDay")
            total_today = stats.get("totalToday")
            max_per_month = stats.get("maxPerMonth")
            total_month = stats.get("totalThisMonth")

            daily_exhausted = False
            monthly_exhausted = False
            try:
                if max_per_day is not None and total_today is not None:
                    daily_exhausted = int(total_today) >= int(max_per_day)
            except Exception:
                daily_exhausted = False
            try:
                if max_per_month is not None and total_month is not None:
                    monthly_exhausted = int(total_month) >= int(max_per_month)
            except Exception:
                monthly_exhausted = False

            if daily_exhausted:
                acct.quota_daily_disabled_until = _end_of_day_ts()
            else:
                acct.quota_daily_disabled_until = 0.0
            acct.quota_monthly_exhausted = bool(monthly_exhausted)

            return "monthly_exhausted" if acct.quota_monthly_exhausted else ("daily_disabled" if daily_exhausted else "ok")

    async def ensure_fresh(self, client: httpx.AsyncClient, acct: Account) -> str:
        return await self._probe_account(client, acct, force=False, ignore_blocks=False, source="request")

    async def poll_once(self, accounts: List[Account]) -> Dict[str, int]:
        counters = {
            "ok": 0,
            "auth": 0,
            "daily_disabled": 0,
            "cooldown": 0,
            "monthly_exhausted": 0,
            "error": 0,
        }
        timeout = httpx.Timeout(20.0, read=20.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for acct in list(accounts):
                try:
                    status = await self._probe_account(
                        client,
                        acct,
                        force=True,
                        ignore_blocks=True,
                        source="session-poll",
                    )
                except Exception:
                    status = "error"
                counters[status if status in counters else "error"] += 1
        await self.persist_state(accounts)
        return counters

    async def mark_cooldown(self, acct: Account) -> None:
        acct.quota_cooldown_until = _now_ts() + self.cooldown_seconds


def load_accounts() -> AccountPool:
    if not ACCOUNTS_PATH.exists():
        raise RuntimeError("Missing bolt_accounts.json")
    data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    accounts = [Account(a) for a in data.get("accounts", [])]
    # Prefer accounts that already have a project_id.
    accounts.sort(key=lambda a: 0 if a.project_id else 1)
    return AccountPool(accounts)


def _accounts_mtime() -> float:
    try:
        return ACCOUNTS_PATH.stat().st_mtime
    except Exception:
        return 0.0


async def reload_accounts_if_changed() -> bool:
    global account_pool, _ACCOUNTS_MTIME
    mtime = _accounts_mtime()
    if not mtime or mtime <= _ACCOUNTS_MTIME:
        return False
    async with _ACCOUNTS_RELOAD_LOCK:
        mtime = _accounts_mtime()
        if not mtime or mtime <= _ACCOUNTS_MTIME:
            return False
        try:
            account_pool = load_accounts()
            _ACCOUNTS_MTIME = mtime
            log_info(f"账号已重载：总账号={len(account_pool.accounts)}")
            return True
        except Exception as exc:
            log_warn(f"账号重载失败：{repr(exc)}")
            return False


def extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") in ("text", "input_text", "output_text"):
            return content.get("text", "")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in ("text", "input_text", "output_text"):
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


def normalize_tool_result_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    updated = text.replace("\\", "/")
    updated = re.sub(r"/tmp/cc-agent/[^/\s]+/project", ".", updated)
    updated = updated.replace("/home/project", ".")
    updated = updated.replace("./.", ".")
    return updated


def build_tool_prompt(tools: List[Dict[str, Any]]) -> str:
    if not tools:
        return ""
    lines = [
        "Tool use is available.",
        "Prefer the environment's native project tools when they can satisfy the request.",
        "Only if native project tools are unavailable or insufficient, respond with exactly one line:",
        "[tool_call] TOOL_NAME({\"arg\": \"value\"})",
        "Do not add any other text around it.",
        "If the user asks to inspect files, list directories, run commands, or read local project state, you must call a tool instead of guessing.",
        "Never claim a tool result without calling the tool first.",
        "After receiving tool results, synthesize them into a direct answer instead of looping on more tools unless another tool call is clearly necessary.",
        "If a path lookup fails, do not keep retrying the same missing path. Fall back to '.' or the latest successful working directory result.",
        "Treat normalized paths like '.' as the current project root.",
        "Available tools:",
    ]
    for tool in tools:
        if tool.get("type") != "function":
            continue
        func = tool.get("function") or {}
        name = func.get("name") or ""
        desc = func.get("description") or ""
        params = func.get("parameters")
        if name:
            if params:
                try:
                    params_str = json.dumps(params, ensure_ascii=False)
                except Exception:
                    params_str = str(params)
                lines.append(f"- {name}: {desc} | params={params_str}")
            else:
                lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


def build_sender(account: Account) -> Optional[Dict[str, Any]]:
    sender = account.sender
    if not sender:
        return None
    required = ["userId", "username", "name", "avatar"]
    if all(k in sender and sender[k] for k in required):
        return sender
    if not any(sender.get(k) for k in required):
        return None
    # Allow partial sender info
    return {k: sender.get(k, "") for k in required}


def make_message(account: Account, content: str, role: str = "user") -> Dict[str, Any]:
    msg_id = secrets.token_urlsafe(12)
    msg = {
        "id": msg_id,
        "role": role,
        "content": content,
        "rawContent": content,
        "cache": False,
        "parts": [],
    }
    if role == "user":
        sender = build_sender(account)
        if sender:
            msg["details"] = {"sender": sender}
    return msg


def build_bolt_messages(account: Account, messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    system_parts: List[str] = []
    tool_name_by_id: Dict[str, str] = {}
    bolt_messages: List[Dict[str, Any]] = []

    for m in messages:
        role = m.get("role")
        content = extract_text_content(m.get("content"))
        if role in ("system", "developer"):
            if content:
                system_parts.append(content)
            continue
        if role == "assistant":
            tool_calls = m.get("tool_calls") or []
            tool_lines: List[str] = []
            for tc in tool_calls:
                if tc.get("type") != "function":
                    continue
                func = tc.get("function") or {}
                name = func.get("name") or ""
                args = func.get("arguments") or ""
                tcid = tc.get("id")
                if tcid and name:
                    tool_name_by_id[tcid] = name
                if name:
                    if args:
                        tool_lines.append(f"[tool_call] {name}({args})")
                    else:
                        tool_lines.append(f"[tool_call] {name}()")
            if tool_lines:
                content = f"{content}\n" if content else ""
                content += "\n".join(tool_lines)
            if content:
                bolt_messages.append(make_message(account, content, role="assistant"))
            continue
        if role == "tool":
            tool_id = m.get("tool_call_id")
            tool_name = tool_name_by_id.get(tool_id or "", "")
            content = normalize_tool_result_text(content)
            label = ""
            if tool_name or tool_id:
                label = f"[tool_result] {tool_name or tool_id}"
            if label:
                content = f"{label}\n{content}" if content else label
            if content:
                bolt_messages.append(make_message(account, content, role="user"))
            continue

        if content:
            bolt_messages.append(make_message(account, content, role="user"))

    return bolt_messages, "\n\n".join(system_parts)


def has_conversation_history(messages: List[Dict[str, Any]]) -> bool:
    user_count = 0
    for m in messages:
        role = m.get("role")
        if role == "user":
            user_count += 1
        if role in ("assistant", "tool"):
            return True
    return user_count > 1


async def fetch_project_id(client: httpx.AsyncClient, account: Account, project_id: Optional[str] = None) -> str:
    if project_id:
        return project_id
    if USE_PROJECT_FORK:
        return await create_project_via_fork(client, account, account.default_model)
    if USE_PROJECT_POOL:
        return await account.acquire_project_id()
    if account.project_id:
        return account.project_id
    resp = await client.get(CHATS_ENDPOINT, headers=bolt_headers(account, project_id=""))
    if resp.status_code == 429:
        try:
            log_warn(f"/api/chats 429 | 账号={account.name} | body={resp.text}")
        except Exception:
            pass
        raise AccountRateLimitError("Rate limited on /api/chats")
    if resp.status_code in (401, 403):
        try:
            log_warn(f"/api/chats 401/403 | 账号={account.name} | body={resp.text}")
        except Exception:
            pass
        raise AccountAuthError("Account not authorized for /api/chats")
    resp.raise_for_status()
    data = resp.json()
    chats = data.get("chats", [])
    if not chats:
        try:
            log_warn(f"/api/chats 空列表 | 账号={account.name} | body={resp.text}")
        except Exception:
            pass
        raise AccountNoProjectError("No chats found. Provide project_id in bolt_accounts.json")
    account.project_id = chats[0].get("projectId") or ""
    if not account.project_id:
        raise RuntimeError("Unable to determine project_id from /api/chats")
    return account.project_id


async def create_project_via_fork(client: httpx.AsyncClient, account: Account, model: str) -> str:
    payload = {"project": {"appFiles": {}}}
    headers = bolt_headers(account, project_id="", selected_model=model)
    try:
        resp = await client.post(f"{BOLT_BASE}/api/projects/sb1/fork", json=payload, headers=headers)
    except Exception as exc:
        raise RuntimeError(f"project fork failed: {exc}") from exc
    if resp.status_code == 429:
        raise AccountRateLimitError("Rate limited on /api/projects/sb1/fork")
    if resp.status_code in (401, 403):
        raise AccountAuthError("Account not authorized for /api/projects/sb1/fork")
    if resp.status_code != 200:
        raise RuntimeError(f"project fork status={resp.status_code} body={resp.text}")
    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"project fork invalid json body={resp.text}") from exc
    project_id = data.get("id") or data.get("projectId") or ""
    if not project_id:
        raise RuntimeError(f"project fork missing id body={resp.text}")
    return str(project_id)


def minimal_project_files() -> Dict[str, Any]:
    return {
        "visible": [],
        "hidden": [
            "/home/project/.bolt/prompt",
            "/home/project/.bolt/config.json",
        ],
    }


async def fetch_template_files(client: httpx.AsyncClient, account: Account, message: str, message_id: str) -> Dict[str, Any]:
    if account.project_files_override:
        return account.project_files_override
    if not account.use_template_files:
        return minimal_project_files()
    if account.cache_template_files and account._cached_template_files:
        return account._cached_template_files
    payload = {"message": message, "messageId": message_id}
    resp = await client.post(TEMPLATE_ENDPOINT, json=payload, headers=bolt_headers(account, project_id=""))
    if resp.status_code == 429:
        raise AccountRateLimitError("Rate limited on /api/template")
    resp.raise_for_status()
    data = resp.json()
    template = data.get("template", {})
    files = template.get("files", {})
    visible = []
    now_ms = int(time.time() * 1000)
    for path, content in files.items():
        visible.append({
            "path": path,
            "content": content,
            "lastModified": now_ms,
            "isBinary": False,
        })
    project_files = {
        "visible": visible,
        "hidden": [
            "/home/project/.bolt/prompt",
            "/home/project/.bolt/config.json",
        ],
    }
    if account.cache_template_files:
        account._cached_template_files = project_files
    return project_files


def bolt_headers(
    account: Account,
    project_id: str,
    selected_model: Optional[str] = None,
    stream: bool = False,
) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Origin": BOLT_BASE,
        "Referer": BOLT_BASE,
        "X-Bolt-Client-Revision": account.bolt_client_revision,
        "User-Agent": BOLT_USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip",
    }
    if stream:
        headers["Accept"] = "text/event-stream, text/plain, */*"
    else:
        headers["Accept"] = "application/json, text/plain, */*"
    if account.cookie:
        headers["Cookie"] = account.cookie
    if project_id:
        headers["X-Bolt-Project-Id"] = project_id
    if selected_model:
        headers["X-Bolt-Selected-Model"] = selected_model
    return headers


def map_finish_reason(reason: str) -> str:
    if not reason:
        return "stop"
    # Pass-through known values
    return reason


def sse_chunk(data: Dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def sse_done() -> bytes:
    return b"data: [DONE]\n\n"


async def openai_full_to_sse(full: Dict[str, Any]) -> AsyncGenerator[bytes, None]:
    request_id = full.get("id") or f"boltcmpl-{secrets.token_hex(8)}"
    created = full.get("created") or int(time.time())
    model = full.get("model") or MODEL_LIST[0]
    usage = full.get("usage")
    choices = full.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason") or "stop"

    yield sse_chunk({
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": message.get("role", "assistant")}}],
    })

    content = message.get("content")
    if content:
        yield sse_chunk({
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": content}}],
        })

    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        yield sse_chunk({
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"tool_calls": tool_calls}}],
        })

    yield sse_chunk({
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        **({"usage": usage} if usage else {}),
    })
    yield sse_done()


def _flatten_text_items(items: Any) -> Optional[str]:
    if not isinstance(items, list):
        return None
    parts: List[str] = []
    for item in items:
        if isinstance(item, str):
            parts.append(item)
            continue
        if isinstance(item, dict):
            text = extract_text_like(item)
            if text:
                parts.append(text)
    return "".join(parts) if parts else None


def extract_text_like(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "delta", "message", "output", "result"):
            if key in value and isinstance(value[key], str):
                return value[key]
        # Some payloads may nest under "data"
        nested = value.get("data") if "data" in value else None
        if isinstance(nested, dict):
            for key in ("text", "content", "delta", "message", "output", "result"):
                if key in nested and isinstance(nested[key], str):
                    return nested[key]
        # Handle list-based content
        for key in ("content", "output", "messages"):
            if key in value and isinstance(value[key], list):
                flat = _flatten_text_items(value[key])
                if flat:
                    return flat
        # OpenAI-like shape
        if isinstance(value.get("choices"), list) and value["choices"]:
            choice = value["choices"][0]
            if isinstance(choice, dict):
                msg = choice.get("message") or {}
                if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                    return msg["content"]
                delta = choice.get("delta") or {}
                if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                    return delta["content"]
    return None


def extract_tool_name(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("toolName", "name", "functionName"):
        name = value.get(key)
        if isinstance(name, str) and name.strip():
            return name.strip()
    function = value.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def normalize_tool_arguments(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def extract_tool_arguments_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("argsText", "argumentsText", "arguments", "args", "input"):
        if key not in value:
            continue
        text = normalize_tool_arguments(value.get(key))
        if text:
            return text
    function = value.get("function")
    if isinstance(function, dict):
        for key in ("arguments", "args"):
            if key not in function:
                continue
            text = normalize_tool_arguments(function.get(key))
            if text:
                return text
    return ""


def get_tool_properties(tool_def: Dict[str, Any]) -> Dict[str, Any]:
    function = tool_def.get("function") or {}
    parameters = function.get("parameters") or {}
    properties = parameters.get("properties") or {}
    return properties if isinstance(properties, dict) else {}


def normalize_bolt_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        return path
    value = path.replace("\\", "/")
    for prefix in ("/home/project",):
        if value == prefix:
            return "."
        if value.startswith(prefix + "/"):
            return value[len(prefix) + 1 :]
    match = re.match(r"^/tmp/cc-agent/[^/]+/project(?:/(.*))?$", value)
    if match:
        rest = match.group(1) or ""
        return rest or "."
    return path


def normalize_bash_command(command: str) -> str:
    if not isinstance(command, str):
        return command
    updated = command
    for root in set(re.findall(r"/tmp/cc-agent/[^/\s]+/project", updated)):
        updated = updated.replace(root, ".")
    updated = updated.replace("/home/project", ".")
    updated = updated.replace("./.", ".")
    return updated


def normalize_shim_tool_call(
    tool_call: Dict[str, str],
    tool_defs: Dict[str, Dict[str, Any]],
) -> Dict[str, str]:
    if not tool_call:
        return tool_call
    args_raw = tool_call.get("arguments") or ""
    if not args_raw:
        return tool_call
    try:
        args = json.loads(args_raw)
    except Exception:
        return tool_call
    if not isinstance(args, dict):
        return tool_call

    name = tool_call.get("name") or ""
    tool_def = tool_defs.get(name) or {}
    kind = infer_declared_tool_kind(name, tool_def)
    normalized = dict(args)

    if kind == "shell":
        for key in ("command", "cmd", "script", "shell_command"):
            if key in normalized and isinstance(normalized[key], str):
                normalized[key] = normalize_bash_command(normalized[key])
    elif kind in ("read_file", "list_dir", "write_file"):
        for key in ("path", "file_path", "filepath", "filename", "dir_path", "directory", "directory_path"):
            if key in normalized and isinstance(normalized[key], str):
                normalized[key] = normalize_bolt_path(normalized[key])

    tool_call["arguments"] = json.dumps(normalized, ensure_ascii=False)
    return tool_call


def infer_declared_tool_kind(name: str, tool_def: Dict[str, Any]) -> str:
    lname = (name or "").lower()
    props = {str(k).lower() for k in get_tool_properties(tool_def).keys()}
    if any(h in lname for h in ("bash", "shell", "terminal", "command", "exec")) or props & {
        "command",
        "cmd",
        "script",
        "shell_command",
    }:
        return "shell"
    if any(h in lname for h in ("list_dir", "listdirectory", "list_directory", "ls", "dir")):
        return "list_dir"
    if any(h in lname for h in ("read_file", "readfile", "open_file", "openfile", "cat", "view")):
        return "read_file"
    if any(h in lname for h in ("write_file", "writefile", "create_file", "edit_file", "save_file", "write")):
        return "write_file"
    if props & {"content", "contents", "text"} and props & {"path", "file_path", "filepath", "filename"}:
        return "write_file"
    if props & {"path", "file_path", "filepath", "filename"} and not (props & {"content", "contents", "text"}):
        return "read_file"
    return ""


def choose_declared_tool_by_kind(tool_defs: Dict[str, Dict[str, Any]], kind: str) -> Optional[str]:
    exact_hints = {
        "shell": ("shell_command", "run_terminal_cmd", "run_command", "bash", "terminal", "shell"),
        "list_dir": ("list_dir", "list_directory", "ls"),
        "read_file": ("read_file", "open_file", "read"),
        "write_file": ("write_file", "create_file", "edit_file", "write"),
    }
    lowered = {name.lower(): name for name in tool_defs.keys()}
    for hint in exact_hints.get(kind, ()):
        if hint in lowered:
            return lowered[hint]
    for name, spec in tool_defs.items():
        if infer_declared_tool_kind(name, spec) == kind:
            return name
    return None


def pick_param_key(tool_def: Dict[str, Any], candidates: Tuple[str, ...]) -> Optional[str]:
    props = get_tool_properties(tool_def)
    lowered = {str(k).lower(): k for k in props.keys()}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None


def map_bolt_native_tool_call(
    tool_name: str,
    raw_args: Dict[str, Any],
    tool_defs: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, str]]:
    if not tool_defs:
        return None

    lname = (tool_name or "").lower()
    if tool_name in tool_defs:
        return {
            "name": tool_name,
            "arguments": json.dumps(raw_args or {}, ensure_ascii=False),
        }

    if lname == "bash":
        target = choose_declared_tool_by_kind(tool_defs, "shell")
        command = normalize_bash_command((raw_args or {}).get("command", ""))
        description = (raw_args or {}).get("description", "")
        if target:
            tool_def = tool_defs[target]
            args: Dict[str, Any] = {}
            cmd_key = pick_param_key(tool_def, ("command", "cmd", "script", "shell_command"))
            if cmd_key:
                args[cmd_key] = command
            desc_key = pick_param_key(tool_def, ("description", "summary", "reason"))
            if desc_key and description:
                args[desc_key] = description
            if args:
                return {"name": target, "arguments": json.dumps(args, ensure_ascii=False)}

        if "ls" in command or "find" in command:
            target = choose_declared_tool_by_kind(tool_defs, "list_dir")
            if target:
                tool_def = tool_defs[target]
                path_key = pick_param_key(tool_def, ("path", "dir_path", "directory", "directory_path"))
                if path_key:
                    match = re.search(r"(?:\s|^)(\./[^\s;&|]+|\.)", command)
                    path = match.group(1) if match else "."
                    return {"name": target, "arguments": json.dumps({path_key: path}, ensure_ascii=False)}

        if "cat " in command:
            target = choose_declared_tool_by_kind(tool_defs, "read_file")
            if target:
                tool_def = tool_defs[target]
                path_key = pick_param_key(tool_def, ("path", "file_path", "filepath", "filename"))
                if path_key:
                    match = re.search(r"cat\s+([^\s;&|]+)", command)
                    if match:
                        return {"name": target, "arguments": json.dumps({path_key: match.group(1)}, ensure_ascii=False)}
        return None

    if lname == "read":
        target = choose_declared_tool_by_kind(tool_defs, "read_file")
        if not target:
            return None
        tool_def = tool_defs[target]
        path_key = pick_param_key(tool_def, ("path", "file_path", "filepath", "filename"))
        if not path_key:
            return None
        file_path = normalize_bolt_path((raw_args or {}).get("file_path", ""))
        return {"name": target, "arguments": json.dumps({path_key: file_path}, ensure_ascii=False)}

    if lname == "write":
        target = choose_declared_tool_by_kind(tool_defs, "write_file")
        if not target:
            return None
        tool_def = tool_defs[target]
        path_key = pick_param_key(tool_def, ("path", "file_path", "filepath", "filename"))
        content_key = pick_param_key(tool_def, ("content", "contents", "text"))
        if not path_key or not content_key:
            return None
        args = {
            path_key: normalize_bolt_path((raw_args or {}).get("file_path", "")),
            content_key: (raw_args or {}).get("content", ""),
        }
        return {"name": target, "arguments": json.dumps(args, ensure_ascii=False)}

    return None


async def parse_bolt_stream(
    resp: httpx.Response,
    debug_ctx: Optional[Dict[str, Any]] = None,
) -> AsyncGenerator[Tuple[str, Any], None]:
    raw_head: List[str] = []
    yielded = False
    buffer = b""

    def _iter_parts(line: str) -> List[str]:
        if "\x1e" in line:
            return [p for p in line.split("\x1e") if p and p.strip()]
        return [line]

    def _parse_part(part: str) -> Optional[Tuple[str, Any]]:
        raw_part = part.rstrip("\r")
        if not raw_part.strip():
            return None
        view = raw_part.lstrip()
        if view.startswith("data:"):
            raw_part = view[5:]
            if raw_part.startswith(" "):
                raw_part = raw_part[1:]
            view = raw_part.lstrip()
        if view == "[DONE]":
            return ("__done__", None)
        try:
            obj = json.loads(raw_part)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            code = obj.get("code") or obj.get("type")
            if code is not None and "value" in obj:
                return (str(code), obj.get("value"))
            text_like = extract_text_like(obj)
            if text_like:
                return ("0", text_like)
        if ":" not in raw_part:
            return None
        code, payload = raw_part.split(":", 1)
        code = code.strip()
        if not code:
            return None
        try:
            value = json.loads(payload)
        except Exception:
            value = payload
        return (code, value)

    ctype = resp.headers.get("content-type", "")
    encoding = (resp.headers.get("content-encoding") or "").lower()
    decompressor = None
    checked_encoding = False
    if "gzip" in encoding:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)

    async def _feed(data: bytes) -> AsyncGenerator[Tuple[str, Any], None]:
        nonlocal buffer, yielded
        if not data:
            return
        buffer += data
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            try:
                text_line = line.decode("utf-8", errors="replace")
            except Exception:
                continue
            if not text_line.strip():
                continue
            if DEBUG_STREAM and len(raw_head) < 10:
                raw_head.append(text_line.rstrip("\r"))
            for part in _iter_parts(text_line):
                parsed = _parse_part(part)
                if not parsed:
                    continue
                if parsed[0] == "__done__":
                    return
                yielded = True
                if debug_ctx:
                    log_debug(
                        f"{debug_ctx.get('trace_id', '-')} upstream event | code={parsed[0]}",
                        event="upstream.stream.event",
                        data={**debug_ctx, "code": parsed[0], "value": parsed[1]},
                    )
                yield parsed

    try:
        async for chunk in resp.aiter_raw():
            if decompressor:
                if not checked_encoding:
                    checked_encoding = True
                    # If already decoded, gzip magic won't be present.
                    if len(chunk) >= 2 and not (chunk[0] == 0x1F and chunk[1] == 0x8B):
                        decompressor = None
                    # If chunk too small, keep decompressor for now.
                if decompressor:
                    try:
                        chunk = decompressor.decompress(chunk)
                    except Exception:
                        chunk = b""
            async for item in _feed(chunk):
                yield item
    except httpx.ReadError:
        if DEBUG_STREAM and not yielded and raw_head:
            log_warn(f"stream read interrupted, no content | content-type={ctype} | head={raw_head}")
        if debug_ctx:
            log_debug(
                f"{debug_ctx.get('trace_id', '-')} upstream read interrupted",
                event="upstream.stream.read_error",
                data={**debug_ctx, "content_type": ctype, "raw_head": raw_head},
            )
        log_warn("upstream stream interrupted (ReadError), treat as done")

    if decompressor:
        try:
            tail = decompressor.flush()
        except Exception:
            tail = b""
        if tail:
            async for item in _feed(tail):
                yield item

    if buffer:
        try:
            text_line = buffer.decode("utf-8", errors="replace")
        except Exception:
            text_line = ""
        if text_line.strip():
            if DEBUG_STREAM and len(raw_head) < 10:
                raw_head.append(text_line.rstrip("\r"))
            for part in _iter_parts(text_line):
                parsed = _parse_part(part)
                if not parsed:
                    continue
                if parsed[0] == "__done__":
                    return
                yielded = True
                if debug_ctx:
                    log_debug(
                        f"{debug_ctx.get('trace_id', '-')} upstream buffered event | code={parsed[0]}",
                        event="upstream.stream.event",
                        data={**debug_ctx, "code": parsed[0], "value": parsed[1], "source": "buffer"},
                    )
                yield parsed

    if DEBUG_STREAM and not yielded and raw_head:
        log_warn(f"stream had no parsable content | content-type={ctype} | head={raw_head}")
    if DEBUG_STREAM and not yielded and not raw_head:
        log_warn(f"no parsable upstream content | content-type={ctype}")
    if debug_ctx and not yielded:
        log_debug(
            f"{debug_ctx.get('trace_id', '-')} upstream stream finished without parsable content",
            event="upstream.stream.empty",
            data={**debug_ctx, "content_type": ctype, "raw_head": raw_head},
        )


def iter_bolt_text_events(text: str) -> Iterable[Tuple[str, Any]]:
    for raw_line in text.splitlines():
        line = raw_line
        if not line.strip():
            continue
        parts = [p for p in line.split("\x1e") if p and p.strip()] if "\x1e" in line else [line]
        for part in parts:
            raw_part = part.rstrip("\r")
            if not raw_part.strip():
                continue
            view = raw_part.lstrip()
            if view.startswith("data:"):
                raw_part = view[5:]
                if raw_part.startswith(" "):
                    raw_part = raw_part[1:]
                view = raw_part.lstrip()
            if view == "[DONE]":
                return
            try:
                obj = json.loads(raw_part)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                code = obj.get("code") or obj.get("type")
                if code is not None and "value" in obj:
                    yield str(code), obj.get("value")
                    continue
                text_like = extract_text_like(obj)
                if text_like:
                    yield "0", text_like
                    continue
            if ":" not in raw_part:
                continue
            code, payload = raw_part.split(":", 1)
            code = code.strip()
            if not code:
                continue
            try:
                value = json.loads(payload)
            except Exception:
                value = payload
            yield code, value


def bolt_text_to_full(text: str, model: str, request_id: str) -> Dict[str, Any]:
    content_parts: List[str] = []
    usage = None
    finish_reason = None

    for code, value in iter_bolt_text_events(text):
        if code == "0":
            if isinstance(value, str):
                piece = strip_bolt_prefix(value)
                content_parts.append(piece)
            else:
                content_parts.append(str(value))
        elif code in ("d", "e"):
            finish_reason = map_finish_reason(value.get("finishReason"))
            if value.get("usage"):
                usage = {
                    "prompt_tokens": value["usage"].get("promptTokens"),
                    "completion_tokens": value["usage"].get("completionTokens"),
                }
                if usage["prompt_tokens"] is not None and usage["completion_tokens"] is not None:
                    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]

    full_text = "".join(content_parts)
    tool_shim = extract_tool_call_from_text(full_text)
    if tool_shim:
        tool_shim = normalize_shim_tool_call(tool_shim, {})
    if tool_shim:
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call_{secrets.token_hex(6)}",
                    "type": "function",
                    "function": {
                        "name": tool_shim["name"],
                        "arguments": tool_shim["arguments"],
                    },
                    "index": 0,
                }
            ],
        }
        finish_reason = "tool_calls"
    else:
        message = {"role": "assistant", "content": full_text}

    resp_obj = {
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason or "stop",
            }
        ],
    }
    if usage:
        resp_obj["usage"] = usage
    return resp_obj


async def read_httpx_text(resp: httpx.Response) -> str:
    try:
        body = await resp.aread()
    except httpx.ReadError:
        return ""
    try:
        return body.decode("utf-8", errors="replace")
    except Exception:
        return ""


async def bolt_stream_to_openai(
    resp: httpx.Response,
    model: str,
    request_id: str,
    tools_enabled: bool = False,
    tool_defs: Optional[Dict[str, Dict[str, Any]]] = None,
    debug_ctx: Optional[Dict[str, Any]] = None,
) -> AsyncGenerator[bytes, None]:
    created = int(time.time())
    tool_index = {}
    tool_args = {}
    usage = None
    sent_role = False
    text_buffer = ""
    emitted_tool_call = False

    def emit_chunk(payload: Dict[str, Any], event: str) -> bytes:
        if debug_ctx:
            log_debug(
                f"{debug_ctx.get('trace_id', '-')} openai stream chunk | event={event}",
                event=f"openai.stream.{event}",
                data={**debug_ctx, "request_id": request_id, "payload": payload},
            )
        return sse_chunk(payload)

    async for code, value in parse_bolt_stream(resp, debug_ctx=debug_ctx):
        if tools_enabled and emitted_tool_call and code != "c":
            yield emit_chunk({
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            }, "finish")
            yield sse_done()
            return
        # Heuristic: handle unknown codes with text-like payloads
        if code not in ("0", "a", "b", "c", "9", "d", "e"):
            text_like = extract_text_like(value)
            if text_like:
                code = "0"
                value = text_like
            else:
                continue
        if code == "0":
            clean = strip_bolt_prefix(value) if isinstance(value, str) else value
            if tools_enabled and isinstance(clean, str) and not emitted_tool_call:
                text_buffer += clean
                tool_call = extract_tool_call_from_text(text_buffer)
                if tool_call:
                    tool_call = normalize_shim_tool_call(tool_call, tool_defs or {})
                    emitted_tool_call = True
                    tool_call_id = f"call_{secrets.token_hex(6)}"
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_call["name"],
                                    "arguments": tool_call["arguments"],
                                },
                            }
                        ]
                    }
                    if not sent_role:
                        delta["role"] = "assistant"
                        sent_role = True
                    yield emit_chunk({
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": delta}],
                    }, "tool_call")
                    yield emit_chunk({
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    }, "finish")
                    yield sse_done()
                    return
                continue
            delta = {"content": clean}
            if not sent_role:
                delta = {"role": "assistant", "content": clean}
                sent_role = True
            yield emit_chunk({
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta}],
            }, "text")
        elif code in ("b", "9"):
            # tool_call_streaming_start or tool_call
            if not tools_enabled:
                continue
            tool_call_id = value.get("toolCallId") or f"call_{secrets.token_hex(6)}"
            raw_tool_name = extract_tool_name(value)
            raw_args = value.get("args") or value.get("arguments") or {}
            mapped = map_bolt_native_tool_call(raw_tool_name, raw_args, tool_defs or {})
            if not tool_call_id or not mapped:
                continue
            if tool_call_id not in tool_index:
                tool_index[tool_call_id] = len(tool_index)
                tool_args[tool_call_id] = ""
            tool_args[tool_call_id] = mapped["arguments"]
            idx = tool_index[tool_call_id]
            emitted_tool_call = True
            delta = {
                "tool_calls": [
                    {
                        "index": idx,
                        "id": tool_call_id,
                        "type": "function",
                        "function": {"name": mapped["name"], "arguments": tool_args[tool_call_id]},
                    }
                ]
            }
            if not sent_role:
                delta["role"] = "assistant"
                sent_role = True
            yield emit_chunk({
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta}],
            }, "tool_call")
            if code == "9":
                yield emit_chunk({
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                }, "finish")
                yield sse_done()
                return
        elif code == "c":
            if not tools_enabled:
                continue
            tool_call_id = value.get("toolCallId")
            args_delta = value.get("argsTextDelta", "")
            if not tool_call_id:
                continue
            if tool_call_id not in tool_index:
                tool_index[tool_call_id] = len(tool_index)
                tool_args[tool_call_id] = ""
            tool_args[tool_call_id] += args_delta
            idx = tool_index[tool_call_id]
            emitted_tool_call = True
            delta = {
                "tool_calls": [
                    {
                        "index": idx,
                        "id": tool_call_id,
                        "type": "function",
                        "function": {"arguments": args_delta},
                    }
                ]
            }
            if not sent_role:
                delta["role"] = "assistant"
                sent_role = True
            yield emit_chunk({
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta}],
            }, "tool_args")
        elif code == "a":
            # Bolt-native tool results are not forwarded; downstream client should
            # execute tool_calls and then send back tool results explicitly.
            continue
        elif code in ("d", "e"):
            finish_reason = map_finish_reason(value.get("finishReason"))
            if value.get("usage"):
                usage = {
                    "prompt_tokens": value["usage"].get("promptTokens"),
                    "completion_tokens": value["usage"].get("completionTokens"),
                }
                if usage["prompt_tokens"] is not None and usage["completion_tokens"] is not None:
                    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
            if tools_enabled and text_buffer and not emitted_tool_call:
                delta = {"content": text_buffer}
                if not sent_role:
                    delta = {"role": "assistant", "content": text_buffer}
                    sent_role = True
                yield emit_chunk({
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta}],
                }, "text")
                text_buffer = ""
            elif not sent_role:
                sent_role = True
                yield emit_chunk({
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}}],
                }, "role")
            yield emit_chunk({
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": ("tool_calls" if emitted_tool_call else finish_reason)}],
                **({"usage": usage} if usage else {}),
            }, "finish")
            yield sse_done()
            return

    # Fallback: if stream ends without explicit finish
    if tools_enabled and text_buffer and not emitted_tool_call:
        delta = {"content": text_buffer}
        if not sent_role:
            delta = {"role": "assistant", "content": text_buffer}
            sent_role = True
        yield emit_chunk({
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta}],
        }, "text")
    elif not sent_role:
        sent_role = True
        yield emit_chunk({
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}}],
        }, "role")
    yield emit_chunk({
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        **({"usage": usage} if usage else {}),
    }, "finish")
    yield sse_done()


async def bolt_stream_to_full(
    resp: httpx.Response,
    model: str,
    request_id: str,
    tools_enabled: bool = False,
    tool_defs: Optional[Dict[str, Dict[str, Any]]] = None,
    debug_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    content_parts = []
    tool_calls: Dict[str, Dict[str, str]] = {}
    usage = None
    finish_reason = None

    async for code, value in parse_bolt_stream(resp, debug_ctx=debug_ctx):
        if code == "0":
            if isinstance(value, str):
                piece = strip_bolt_prefix(value)
                content_parts.append(piece)
            else:
                content_parts.append(str(value))
        elif code in ("b", "9"):
            if not tools_enabled:
                continue
            tool_call_id = value.get("toolCallId") or f"call_{secrets.token_hex(6)}"
            raw_tool_name = extract_tool_name(value)
            raw_args = value.get("args") or value.get("arguments") or {}
            mapped = map_bolt_native_tool_call(raw_tool_name, raw_args, tool_defs or {})
            if not mapped:
                continue
            tool_entry = tool_calls.setdefault(tool_call_id, {"name": mapped["name"], "arguments": ""})
            tool_entry["name"] = mapped["name"]
            tool_entry["arguments"] = mapped["arguments"]
            if code == "9":
                break
        elif code == "c":
            if not tools_enabled:
                continue
            tool_call_id = value.get("toolCallId")
            if not tool_call_id or tool_call_id not in tool_calls:
                continue
            args_delta = value.get("argsTextDelta", "")
            tool_calls[tool_call_id]["arguments"] += args_delta
        elif code == "a":
            if tool_calls:
                break
            continue
        elif code in ("d", "e"):
            finish_reason = map_finish_reason(value.get("finishReason"))
            if value.get("usage"):
                usage = {
                    "prompt_tokens": value["usage"].get("promptTokens"),
                    "completion_tokens": value["usage"].get("completionTokens"),
                }
                if usage["prompt_tokens"] is not None and usage["completion_tokens"] is not None:
                    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
            if tool_calls:
                break

    full_text = "".join(content_parts)
    tool_shim = extract_tool_call_from_text(full_text) if tools_enabled else None
    if tool_shim:
        tool_shim = normalize_shim_tool_call(tool_shim, tool_defs or {})
    if tool_calls:
        message = {"role": "assistant", "content": ""}
        tc_list = []
        for idx, (tcid, tc) in enumerate(tool_calls.items()):
            tc_list.append({
                "id": tcid,
                "type": "function",
                "function": {"name": tc.get("name") or "", "arguments": tc.get("arguments") or ""},
                "index": idx,
            })
        message["tool_calls"] = tc_list
        finish_reason = "tool_calls"
    elif tool_shim:
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call_{secrets.token_hex(6)}",
                    "type": "function",
                    "function": {
                        "name": tool_shim["name"],
                        "arguments": tool_shim["arguments"],
                    },
                    "index": 0,
                }
            ],
        }
        finish_reason = "tool_calls"
    else:
        message = {"role": "assistant", "content": full_text}

    resp_obj = {
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason or "stop",
            }
        ],
    }
    if usage:
        resp_obj["usage"] = usage
    if debug_ctx:
        log_debug(
            f"{debug_ctx.get('trace_id', '-')} openai full response ready",
            event="openai.response.full",
            data={**debug_ctx, "request_id": request_id, "response": resp_obj},
        )
    return resp_obj


quota_manager = QuotaManager(
    state_path=QUOTA_STATE_PATH,
    refresh_seconds=QUOTA_REFRESH_SECONDS,
    cooldown_seconds=QUOTA_COOLDOWN_SECONDS,
)
app = FastAPI()
account_pool = load_accounts()
_ACCOUNTS_MTIME = _accounts_mtime()
session_poll_task: Optional[asyncio.Task] = None


async def session_poll_loop() -> None:
    if BOLT_SESSION_POLL_SECONDS <= 0:
        log_info("session 轮询已关闭")
        return
    log_info(f"session 轮询启动：间隔={BOLT_SESSION_POLL_SECONDS}s")
    while True:
        try:
            counters = await quota_manager.poll_once(account_pool.accounts)
            for acct in list(account_pool.accounts):
                if acct.quota_monthly_exhausted:
                    await account_pool.remove_account(acct, reason="monthly_exhausted")
                    log_warn(f"session 轮询移除总限额耗尽账号：{acct.name}")
            log_info(
                "session 轮询完成："
                f"ok={counters.get('ok', 0)} "
                f"auth={counters.get('auth', 0)} "
                f"daily={counters.get('daily_disabled', 0)} "
                f"cooldown={counters.get('cooldown', 0)} "
                f"monthly={counters.get('monthly_exhausted', 0)} "
                f"error={counters.get('error', 0)}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_warn(f"session 轮询异常：{repr(e)}")
        await asyncio.sleep(max(5, BOLT_SESSION_POLL_SECONDS))


def _startup_report() -> None:
    now = _now_ts()
    total = len(account_pool.accounts)
    with_project = sum(1 for a in account_pool.accounts if a.project_id)
    usable = sum(
        1
        for a in account_pool.accounts
        if a.project_id
        and not a.quota_monthly_exhausted
        and not (a._bad_until and now < a._bad_until)
        and not (a.quota_daily_disabled_until and now < a.quota_daily_disabled_until)
        and not (a.quota_cooldown_until and now < a.quota_cooldown_until)
    )
    missing_project = total - with_project
    log_info(f"启动完成：总账号={total} 可用账号={max(usable,0)} 已绑定项目={with_project} 缺项目={missing_project}")
    log_info(
        f"配置：鉴权={'开启' if AUTH_KEY else '关闭'} 配额刷新={QUOTA_REFRESH_SECONDS}s 冷却={QUOTA_COOLDOWN_SECONDS}s"
    )
    log_info(
        f"调试：DEBUG={'开启' if BOLT_DEBUG_MODE else '关闭'} STDOUT={BOLT_STDOUT_LOG_MODE} LOG={BOLT_DEBUG_LOG_PATH}"
    )
    log_info(f"session 轮询：间隔={BOLT_SESSION_POLL_SECONDS}s")


_startup_report()


@app.on_event("startup")
async def _app_startup() -> None:
    global session_poll_task
    if BOLT_SESSION_POLL_SECONDS > 0 and session_poll_task is None:
        session_poll_task = asyncio.create_task(session_poll_loop())


@app.on_event("shutdown")
async def _app_shutdown() -> None:
    global session_poll_task
    if session_poll_task is not None:
        session_poll_task.cancel()
        try:
            await session_poll_task
        except asyncio.CancelledError:
            pass
        session_poll_task = None


def require_auth(req: Request) -> None:
    if not AUTH_KEY:
        return
    auth = req.headers.get("authorization", "")
    if auth != f"Bearer {AUTH_KEY}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/v1/models")
async def list_models():
    data = [
        {
            "id": mid,
            "object": "model",
            "created": 0,
            "owned_by": "bolt",
        }
        for mid in MODEL_LIST
    ]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    require_auth(req)
    try:
        await reload_accounts_if_changed()
    except Exception:
        pass
    payload = await req.json()
    trace_id = f"req-{secrets.token_hex(6)}"
    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages must be a list")
    stream = bool(payload.get("stream", False))
    tools = payload.get("tools") or []
    declared_tools = {
        (tool.get("function") or {}).get("name"): tool
        for tool in tools
        if tool.get("type") == "function" and (tool.get("function") or {}).get("name")
    }
    meta = payload.get("metadata") or {}
    project_id_override = meta.get("project_id") or payload.get("project_id")
    session_id = _normalize_session_id(
        meta.get("session_id")
        or req.headers.get("x-session-id")
        or req.headers.get("x-chat-session-id")
        or req.headers.get("x-conversation-id")
    )
    new_session = bool(meta.get("new_session") or meta.get("reset_session"))
    if session_id and not project_id_override:
        try:
            session_project_id = await get_session_project_id(session_id, reset=new_session)
            if session_project_id:
                project_id_override = session_project_id
        except Exception:
            pass
    framework = meta.get("framework")
    prompt_mode = meta.get("prompt_mode")
    project_prompt = meta.get("project_prompt")
    extra_system_prompt = meta.get("global_system_prompt") or ""

    requested_model = payload.get("model")
    log_debug(
        f"{trace_id} incoming chat request | stream={stream} messages={len(messages)} tools={len(declared_tools)}",
        event="openai.request.in",
        data={
            "trace_id": trace_id,
            "path": str(req.url.path),
            "query": str(req.url.query),
            "headers": dict(req.headers),
            "payload": payload,
            "session_id": session_id,
            "new_session": new_session,
        },
    )

    async def build_bolt_body(
        client: httpx.AsyncClient,
        account: Account,
        model: str,
    ) -> Tuple[Dict[str, Any], str]:
        bolt_messages_local, system_prompt_local = build_bolt_messages(account, messages)
        if not bolt_messages_local:
            raise HTTPException(status_code=400, detail="No usable messages provided")
        last_user = None
        for m in reversed(messages):
            if m.get("role") != "user":
                continue
            last_user_text = extract_text_content(m.get("content"))
            if not last_user_text:
                continue
            last_user = {
                "content": last_user_text,
                "id": m.get("id") or secrets.token_urlsafe(12),
            }
            break
        if not last_user:
            raise HTTPException(status_code=400, detail="No user message provided")

        project_id = await fetch_project_id(client, account, project_id_override)
        if ALWAYS_NEW_PROJECT:
            is_first_prompt = not has_conversation_history(messages)
        else:
            is_first_prompt = project_id not in account._project_seen
            account._project_seen.add(project_id)

        project_files = await fetch_template_files(client, account, last_user["content"], last_user["id"])
        tool_prompt = build_tool_prompt(tools) if tools else ""
        global_system_prompt = "\n\n".join(
            [p for p in [account.global_system_prompt, system_prompt_local, extra_system_prompt, tool_prompt] if p]
        )

        bolt_body = {
            "messages": bolt_messages_local,
            "isFirstPrompt": is_first_prompt,
            "featurePreviews": {
                "reasoning": ENABLE_REASONING,
                "diffs": False,
                "imageGeneration": False,
            },
            "errorReasoning": None,
            "framework": framework or account.framework,
            "promptMode": prompt_mode or account.prompt_mode,
            "selectedModel": model,
            "projectId": project_id,
            "stripeStatus": "not-configured",
            "usesInspectedElement": False,
            "runningCommands": [],
            "projectFiles": project_files,
            "globalSystemPrompt": global_system_prompt,
            "projectPrompt": project_prompt or account.project_prompt,
            "dependencies": [],
            "hostingProvider": "bolt",
            "problems": "",
            "id": secrets.token_urlsafe(8),
            "streamProtocol": "data",
            "headers": {},
            "requestType": "generate",
        }
        log_debug(
            f"{trace_id} bolt body prepared | account={account.name} model={model} project={project_id}",
            event="bolt.request.prepared",
            data={
                "trace_id": trace_id,
                "account": account.name,
                "model": model,
                "project_id": project_id,
                "project_id_override": project_id_override,
                "openai_messages": messages,
                "bolt_messages": bolt_messages_local,
                "declared_tools": declared_tools,
                "tool_prompt": tool_prompt,
                "global_system_prompt": global_system_prompt,
                "bolt_body": bolt_body,
            },
        )
        return bolt_body, project_id

    async def select_account_and_body() -> Tuple[Account, Dict[str, Any], str, str]:
        last_err: Optional[Exception] = None
        counters = {
            "daily_disabled": 0,
            "cooldown": 0,
            "monthly_exhausted": 0,
            "auth": 0,
            "no_project": 0,
            "other": 0,
        }
        timeout = httpx.Timeout(30.0, read=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            prefer_project = not project_id_override
            for _ in range(len(account_pool.accounts)):
                acct = await account_pool.acquire_account(prefer_project_id=prefer_project)
                model = requested_model or acct.default_model
                if model not in MODEL_LIST:
                    await acct.release()
                    raise HTTPException(status_code=400, detail=f"Unknown model: {model}")
                try:
                    quota_status = await quota_manager.ensure_fresh(client, acct)
                    await quota_manager.persist_state(account_pool.accounts)
                    if quota_status == "monthly_exhausted":
                        await acct.release()
                        await account_pool.remove_account(acct, reason="monthly_exhausted")
                        log_warn(f"account monthly exhausted, removed: {acct.name}")
                        counters["monthly_exhausted"] += 1
                        last_err = AccountNoProjectError("Account monthly exhausted")
                        continue
                    if quota_status in ("daily_disabled", "cooldown", "auth", "error"):
                        if quota_status == "auth":
                            await schedule_auth_refresh(acct, reason="quota-auth")
                        await acct.release()
                        log_warn(f"account unavailable ({quota_status}): {acct.name}")
                        if quota_status in counters:
                            counters[quota_status] += 1
                        else:
                            counters["other"] += 1
                        last_err = AccountRateLimitError(f"Account quota blocked: {quota_status}")
                        continue
                    bolt_body, project_id = await build_bolt_body(client, acct, model)
                    log_debug(
                        f"{trace_id} account selected | account={acct.name} model={model} project={project_id}",
                        event="account.selected",
                        data={
                            "trace_id": trace_id,
                            "account": acct.name,
                            "model": model,
                            "project_id": project_id,
                        },
                    )
                    return acct, bolt_body, project_id, model
                except AccountAuthError as e:
                    acct.mark_bad()
                    await quota_manager.persist_state(account_pool.accounts)
                    await acct.release()
                    log_warn(f"account auth failed: {acct.name}")
                    await schedule_auth_refresh(acct, reason="account-auth")
                    counters["auth"] += 1
                    last_err = e
                    continue
                except AccountRateLimitError as e:
                    acct.mark_bad(seconds=60)
                    await acct.release()
                    log_warn(f"account rate-limited: {acct.name}")
                    counters["cooldown"] += 1
                    last_err = e
                    continue
                except httpx.HTTPStatusError as e:
                    status = getattr(e.response, "status_code", None)
                    if status in (401, 403):
                        acct.mark_bad()
                        await quota_manager.persist_state(account_pool.accounts)
                        await acct.release()
                        log_warn(f"upstream 401/403, disable account: {acct.name}")
                        await schedule_auth_refresh(acct, reason="upstream-401")
                        counters["auth"] += 1
                        last_err = e
                        continue
                    if status == 429:
                        acct.mark_bad(seconds=60)
                        await acct.release()
                        log_warn(f"upstream 429, cooldown account: {acct.name}")
                        counters["cooldown"] += 1
                        last_err = e
                        continue
                    await acct.release()
                    log_error(f"upstream error {status}, account={acct.name}")
                    counters["other"] += 1
                    raise
                except AccountNoProjectError as e:
                    await acct.release()
                    counters["no_project"] += 1
                    last_err = e
                    continue
                except Exception:
                    await acct.release()
                    counters["other"] += 1
                    raise
        log_error(f"no usable accounts: {last_err} | counts={counters}")
        raise HTTPException(
            status_code=400,
            detail={
                "error": "no_usable_accounts",
                "last_error": str(last_err),
                "counts": counters,
            },
        )

    max_auth_retries = max(1, min(3, len(account_pool.accounts)))
    attempt = 0

    while True:
        acct, bolt_body, project_id, model = await select_account_and_body()
        request_id = f"boltcmpl-{secrets.token_hex(8)}"

        async def open_upstream_stream() -> Tuple[httpx.AsyncClient, httpx.Response]:
            timeout = httpx.Timeout(connect=30.0, write=30.0, read=None, pool=30.0)
            client = httpx.AsyncClient(timeout=timeout)
            headers = bolt_headers(acct, project_id=project_id, selected_model=model, stream=True)
            request = client.build_request("POST", CHAT_ENDPOINT, json=bolt_body, headers=headers)
            log_debug(
                f"{trace_id} -> upstream | account={acct.name} model={model} project={project_id}",
                event="upstream.request.out",
                data={
                    "trace_id": trace_id,
                    "request_id": request_id,
                    "account": acct.name,
                    "method": "POST",
                    "url": CHAT_ENDPOINT,
                    "headers": headers,
                    "body": bolt_body,
                },
            )
            try:
                resp = await client.send(request, stream=True)
                log_debug(
                    f"{trace_id} upstream response started | status={resp.status_code}",
                    event="upstream.response.start",
                    data={
                        "trace_id": trace_id,
                        "request_id": request_id,
                        "account": acct.name,
                        "status_code": resp.status_code,
                        "headers": dict(resp.headers),
                    },
                )
                return client, resp
            except Exception:
                await client.aclose()
                raise

        client, resp = await open_upstream_stream()
        try:
            if resp.status_code == 429:
                body = await read_httpx_text(resp)
                log_debug(
                    f"{trace_id} upstream 429 | account={acct.name}",
                    event="upstream.response.error",
                    data={
                        "trace_id": trace_id,
                        "request_id": request_id,
                        "account": acct.name,
                        "status_code": resp.status_code,
                        "headers": dict(resp.headers),
                        "body": body,
                    },
                )
                log_warn(f"upstream 429 rate-limited | account={acct.name} | body={body}")
                await quota_manager.mark_cooldown(acct)
                await quota_manager.persist_state(account_pool.accounts)
                raise HTTPException(status_code=429, detail="Bolt rate limited. Try later.")
            if resp.status_code in (401, 403):
                body = await read_httpx_text(resp)
                log_debug(
                    f"{trace_id} upstream auth error | account={acct.name}",
                    event="upstream.response.error",
                    data={
                        "trace_id": trace_id,
                        "request_id": request_id,
                        "account": acct.name,
                        "status_code": resp.status_code,
                        "headers": dict(resp.headers),
                        "body": body,
                    },
                )
                log_warn(f"upstream 401/403 unauthorized | account={acct.name} | body={body}")
                acct.mark_bad()
                await quota_manager.persist_state(account_pool.accounts)
                await schedule_auth_refresh(acct, reason="upstream-401")
                attempt += 1
                await resp.aclose()
                await client.aclose()
                await acct.release()
                if attempt < max_auth_retries:
                    continue
                raise HTTPException(status_code=401, detail="Bolt session unauthorized.")
            if resp.status_code >= 400:
                body = await read_httpx_text(resp)
                log_debug(
                    f"{trace_id} upstream http error | account={acct.name} status={resp.status_code}",
                    event="upstream.response.error",
                    data={
                        "trace_id": trace_id,
                        "request_id": request_id,
                        "account": acct.name,
                        "status_code": resp.status_code,
                        "headers": dict(resp.headers),
                        "body": body,
                    },
                )
                log_error(f"upstream error {resp.status_code} | account={acct.name} | body={body}")
                raise HTTPException(status_code=resp.status_code, detail="Bolt upstream error.")
        except Exception:
            await resp.aclose()
            await client.aclose()
            await acct.release()
            raise

        if stream:
            async def stream_generator() -> AsyncGenerator[bytes, None]:
                try:
                    async for chunk in bolt_stream_to_openai(
                        resp,
                        model,
                        request_id,
                        tools_enabled=bool(tools),
                        tool_defs=declared_tools,
                        debug_ctx={
                            "trace_id": trace_id,
                            "account": acct.name,
                            "model": model,
                            "project_id": project_id,
                        },
                    ):
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()
                    await acct.release()

            return StreamingResponse(stream_generator(), media_type="text/event-stream")

        try:
            full = await bolt_stream_to_full(
                resp,
                model,
                request_id,
                tools_enabled=bool(tools),
                tool_defs=declared_tools,
                debug_ctx={
                    "trace_id": trace_id,
                    "account": acct.name,
                    "model": model,
                    "project_id": project_id,
                },
            )
            return JSONResponse(full)
        finally:
            await resp.aclose()
            await client.aclose()
            await acct.release()


@app.get("/")
async def root(req: Request):
    require_auth(req)
    return {"status": "ok", "endpoints": ["/v1/models", "/v1/chat/completions"]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
