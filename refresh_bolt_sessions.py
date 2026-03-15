# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "requests",
# ]
# ///

import argparse
import asyncio
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

load_env_lock = threading.Lock()
account_file_lock = threading.Lock()
print_lock = threading.Lock()
log_prefix = ContextVar("refresh_log_prefix", default="")

ACCOUNTS_PATH = Path("bolt_accounts.json")
BOLT_ENV_PATH = Path(".env")
BOLT_BASE = "https://bolt.new"
STACKBLITZ_BASE = "https://stackblitz.com"
DEFAULT_CLIENT_REVISION = "f05eb54"
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def load_local_env(path: Path = BOLT_ENV_PATH) -> None:
    if not path.exists():
        return
    with load_env_lock:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


load_local_env()


if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else default



def env_int(name: str, default: int) -> int:
    raw = env(name, str(default))
    try:
        return int(raw)
    except Exception:
        return default



def clip_text(value: Any, limit: int = 300) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f" ...(+{len(text) - limit} chars)"



def log(step: str, message: str = "") -> None:
    prefix = log_prefix.get("")
    label = f"{prefix}/{step}" if prefix else step
    with print_lock:
        if message:
            print(f"[{label}] {message}")
        else:
            print(f"[{label}]")



def log_warn(message: str) -> None:
    log("??", message)



def log_err(message: str) -> None:
    log("??", message)



def mask_secret(text: str) -> str:
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:3]}***{text[-2:]}"



def parse_iso_ts(value: Optional[str]) -> float:
    if not value:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return 0.0



def account_name_from_email(email: str) -> str:
    local = email.split("@", 1)[0]
    return f"account-{local}"



def extract_session_value(cookie_value: str) -> str:
    value = (cookie_value or "").strip()
    if value.startswith("__session="):
        value = value.split("=", 1)[1]
    if ";" in value:
        value = value.split(";", 1)[0]
    return value.strip()



def build_signin_url(authorize_uri: str) -> str:
    parsed = urllib.parse.urlparse(authorize_uri)
    redirect_to = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    encoded = urllib.parse.quote(redirect_to, safe="")
    return f"{STACKBLITZ_BASE}/sign_in?redirect_to={encoded}"



def load_accounts_payload() -> Dict[str, Any]:
    if not ACCOUNTS_PATH.exists():
        return {"accounts": [], "rotation": "round_robin"}
    try:
        data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("accounts", [])
            data.setdefault("rotation", "round_robin")
            return data
    except Exception:
        pass
    return {"accounts": [], "rotation": "round_robin"}



def save_accounts_payload(payload: Dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    ACCOUNTS_PATH.write_text(text, encoding="utf-8")



def count_auth_bad_accounts(accounts: List[Dict[str, Any]]) -> int:
    now = time.time()
    total = 0
    for item in accounts:
        quota = item.get("quota") or {}
        if parse_iso_ts(quota.get("auth_disabled_until")) > now:
            total += 1
    return total



def pick_targets(args: argparse.Namespace) -> List[Dict[str, Any]]:
    payload = load_accounts_payload()
    accounts = payload.get("accounts") or []
    by_name = {item.get("name") or "": item for item in accounts}

    if args.account or args.email:
        selected: Dict[str, Any] = {}
        if args.account and args.account in by_name:
            selected.update(by_name[args.account])
        if args.email:
            selected["email"] = args.email.strip()
            selected.setdefault("name", args.account or account_name_from_email(args.email.strip()))
        if args.password:
            selected["password"] = args.password.strip()
        if not selected:
            return []
        return [selected]

    if args.all:
        return accounts

    now = time.time()
    targets = []
    for item in accounts:
        quota = item.get("quota") or {}
        auth_bad_until = parse_iso_ts(quota.get("auth_disabled_until"))
        cookie = extract_session_value(item.get("cookie") or "")
        if auth_bad_until > now or not cookie:
            targets.append(item)
    return targets



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="????? Bolt ?? session")
    parser.add_argument("--account", default="", help="????????")
    parser.add_argument("--email", default="", help="??????????")
    parser.add_argument("--password", default="", help="??????????")
    parser.add_argument("--all", action="store_true", help="??????????????")
    parser.add_argument("--concurrency", type=int, default=env_int("BOLT_REFRESH_CONCURRENCY", 2))
    parser.add_argument(
        "--request-timeout",
        "--oauth-timeout",
        dest="request_timeout",
        type=int,
        default=env_int("BOLT_REFRESH_REQUEST_TIMEOUT", env_int("BOLT_REFRESH_OAUTH_TIMEOUT", 30)),
        help="????????",
    )
    return parser.parse_args()



def session_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": env("BOLT_USER_AGENT", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT,
    }
    if extra:
        headers.update(extra)
    return headers



def start_bolt_oauth(sess: requests.Session, timeout: int) -> Tuple[str, str]:
    rid = secrets.token_urlsafe(12)
    payload = {
        "start": {"destination": "https://bolt.new/", "rid": rid},
        "upgrade": False,
        "ssoFlow": False,
    }
    resp = sess.post(
        f"{BOLT_BASE}/api/sessions",
        json=payload,
        headers=session_headers(
            {
                "content-type": "application/json",
                "origin": BOLT_BASE,
                "referer": f"{BOLT_BASE}/",
            }
        ),
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    authorize_uri = data.get("authorizeUri") or ""
    oauth_cookie = sess.cookies.get("__oauth", domain="bolt.new") or sess.cookies.get("__oauth") or ""
    return authorize_uri, oauth_cookie



def open_stackblitz_signin(sess: requests.Session, signin_url: str, timeout: int) -> str:
    resp = sess.get(
        signin_url,
        headers=session_headers(
            {
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "referer": f"{BOLT_BASE}/",
                "origin": BOLT_BASE,
            }
        ),
        timeout=timeout,
    )
    resp.raise_for_status()
    csrf = sess.cookies.get("CSRF-TOKEN", domain="stackblitz.com") or sess.cookies.get("CSRF-TOKEN") or ""
    if not csrf:
        raise RuntimeError("??? StackBlitz CSRF-TOKEN")
    return csrf



def probe_sso(sess: requests.Session, signin_url: str, email: str, timeout: int) -> Dict[str, Any]:
    resp = sess.get(
        f"{STACKBLITZ_BASE}/api/users/sessions/sso",
        params={"login": email},
        headers=session_headers(
            {
                "origin": STACKBLITZ_BASE,
                "referer": signin_url,
            }
        ),
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("SSO ???? JSON ??")
    return data



def login_stackblitz_password(
    sess: requests.Session,
    signin_url: str,
    email: str,
    password: str,
    csrf_token: str,
    timeout: int,
) -> None:
    resp = sess.post(
        f"{STACKBLITZ_BASE}/api/users/sessions",
        json={"user": {"login": email, "password": password}},
        headers=session_headers(
            {
                "content-type": "application/json",
                "origin": STACKBLITZ_BASE,
                "referer": signin_url,
                "x-csrf-token": csrf_token,
            }
        ),
        timeout=timeout,
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"?????? status={resp.status_code} body={clip_text(resp.text)}")



def follow_authorize(sess: requests.Session, authorize_uri: str, signin_url: str, timeout: int) -> str:
    resp = sess.get(
        authorize_uri,
        allow_redirects=True,
        headers=session_headers(
            {
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "referer": signin_url,
            }
        ),
        timeout=timeout,
    )
    final_url = resp.url
    if not final_url.startswith(f"{BOLT_BASE}/oauth2?") or "code=" not in final_url:
        raise RuntimeError(f"OAuth ???? final_url={clip_text(final_url)}")
    return final_url



def finish_bolt_session(
    sess: requests.Session,
    referer_url: str,
    client_revision: str,
    model: str,
    timeout: int,
) -> str:
    resp = sess.post(
        f"{BOLT_BASE}/api/sessions",
        json={"finish": True, "upgrade": False},
        headers=session_headers(
            {
                "content-type": "application/json",
                "origin": BOLT_BASE,
                "referer": referer_url,
                "x-bolt-client-revision": client_revision,
                "x-bolt-selected-model": model,
            }
        ),
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Bolt finish ?? status={resp.status_code} body={clip_text(resp.text)}")
    session_cookie = sess.cookies.get("__session", domain="bolt.new") or sess.cookies.get("__session") or ""
    session_cookie = extract_session_value(session_cookie)
    if not session_cookie:
        raise RuntimeError("Bolt finish ??????? __session")
    return session_cookie



def probe_token_stats(session_cookie: str, client_revision: str, model: str, timeout: int) -> Tuple[int, Dict[str, Any], str]:
    resp = requests.get(
        f"{BOLT_BASE}/api/token-stats",
        headers=session_headers(
            {
                "cookie": f"__session={session_cookie}",
                "origin": BOLT_BASE,
                "referer": f"{BOLT_BASE}/",
                "x-bolt-client-revision": client_revision,
                "x-bolt-selected-model": model,
            }
        ),
        timeout=timeout,
    )
    body = resp.text or ""
    data: Dict[str, Any] = {}
    try:
        parsed = resp.json()
        if isinstance(parsed, dict):
            data = parsed
    except Exception:
        data = {}
    return resp.status_code, data, body



def fetch_project_id(session_cookie: str, client_revision: str, model: str, timeout: int) -> Tuple[str, bool]:
    resp = requests.get(
        f"{BOLT_BASE}/api/chats",
        headers=session_headers(
            {
                "cookie": f"__session={session_cookie}",
                "origin": BOLT_BASE,
                "referer": f"{BOLT_BASE}/",
                "x-bolt-client-revision": client_revision,
                "x-bolt-selected-model": model,
            }
        ),
        timeout=timeout,
    )
    if resp.status_code != 200:
        return "", False
    try:
        data = resp.json()
    except Exception:
        return "", False
    chats = data.get("chats") or []
    if not chats:
        return "", True
    return str(chats[0].get("projectId") or ""), True



def create_project_via_stackblitz(
    sess: requests.Session,
    client_revision: str,
    model: str,
    timeout: int,
) -> str:
    csrf = sess.cookies.get("CSRF-TOKEN", domain="stackblitz.com") or sess.cookies.get("CSRF-TOKEN") or ""
    headers = session_headers(
        {
            "content-type": "application/json",
            "origin": BOLT_BASE,
            "referer": f"{BOLT_BASE}/",
            "x-bolt-client-revision": client_revision,
            "x-bolt-selected-model": model,
        }
    )
    if csrf:
        headers["x-csrf-token"] = csrf
    resp = sess.post(
        f"{STACKBLITZ_BASE}/api/projects/sb1/fork",
        headers=headers,
        json={"project": {"appFiles": {}}},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"?????? status={resp.status_code} body={clip_text(resp.text)}")
    data = resp.json()
    project_id = data.get("id") or data.get("projectId") or ""
    if not project_id:
        raise RuntimeError(f"????????? id body={clip_text(resp.text)}")
    return str(project_id)



def persist_success(
    account_name: str,
    email: str,
    password: str,
    session_cookie: str,
    project_id: str,
    client_revision: str,
    model: str,
) -> None:
    with account_file_lock:
        payload = load_accounts_payload()
        accounts = payload.get("accounts") or []
        target = None
        full_cookie = f"__session={session_cookie}"
        for item in accounts:
            if item.get("name") == account_name or item.get("email") == email:
                target = item
                break
        if target is None:
            target = {
                "name": account_name,
                "sender": {"userId": "", "username": "", "name": "", "avatar": ""},
                "bolt_client_revision": client_revision,
                "default_model": model,
                "max_concurrency": 2,
            }
            accounts.append(target)
        target["name"] = account_name
        target["email"] = email
        if password:
            target["password"] = password
        target["cookie"] = full_cookie
        if project_id:
            target["project_id"] = project_id
        else:
            target.setdefault("project_id", "")
        target.setdefault("sender", {"userId": "", "username": "", "name": "", "avatar": ""})
        target["bolt_client_revision"] = client_revision or target.get("bolt_client_revision") or DEFAULT_CLIENT_REVISION
        target["default_model"] = model or target.get("default_model") or DEFAULT_MODEL
        target.setdefault("max_concurrency", 2)
        quota = target.get("quota") or {}
        had_auth_bad = bool(quota.get("auth_disabled_until"))
        quota["auth_disabled_until"] = None
        if had_auth_bad:
            quota["cooldown_until"] = None
        target["quota"] = quota
        payload["accounts"] = accounts
        save_accounts_payload(payload)



def refresh_one_sync(args: argparse.Namespace, record: Dict[str, Any], worker_label: str = "") -> Dict[str, Any]:
    token = log_prefix.set(worker_label or "")
    try:
        email = (args.email or record.get("email") or "").strip()
        password = (args.password or record.get("password") or "").strip()
        account_name = (record.get("name") or args.account or account_name_from_email(email)).strip()
        client_revision = (record.get("bolt_client_revision") or DEFAULT_CLIENT_REVISION).strip() or DEFAULT_CLIENT_REVISION
        model = (record.get("default_model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        old_project_id = (record.get("project_id") or "").strip()

        if not email or not password:
            reason = "?? email/password"
            log_warn(f"?? {account_name}?{reason}")
            return {"success": False, "name": account_name, "email": email, "reason": reason}

        timeout = max(5, int(args.request_timeout))

        log("??", f"??={account_name}")
        log("??", f"??={email}")
        log("??", f"??={mask_secret(password)}")

        sess = requests.Session()

        log("??", "1/6 ?? Bolt OAuth")
        authorize_uri, oauth_cookie = start_bolt_oauth(sess, timeout)
        if not authorize_uri or not oauth_cookie:
            raise RuntimeError("Bolt start ??? authorizeUri/__oauth")

        signin_url = build_signin_url(authorize_uri)
        log("??", "2/6 ?? StackBlitz ???")
        csrf_token = open_stackblitz_signin(sess, signin_url, timeout)

        log("??", "3/6 ?? SSO")
        sso_info = probe_sso(sess, signin_url, email, timeout)
        if sso_info.get("forceSSO"):
            raise RuntimeError(f"????? SSO???????: {clip_text(sso_info)}")

        log("??", "4/6 ??????")
        login_stackblitz_password(sess, signin_url, email, password, csrf_token, timeout)

        log("??", "5/6 ?? Bolt ??")
        final_oauth_url = follow_authorize(sess, authorize_uri, signin_url, timeout)

        log("??", "6/6 ?? Bolt ??")
        session_cookie = finish_bolt_session(sess, final_oauth_url, client_revision, model, timeout)

        stats_status, stats_payload, stats_body = probe_token_stats(session_cookie, client_revision, model, timeout)
        if stats_status in (401, 403):
            raise RuntimeError(f"? session ???? token-stats status={stats_status} body={clip_text(stats_body)}")
        if stats_status != 200:
            log_warn(f"token-stats ? 200?status={stats_status} body={clip_text(stats_body)}")

        project_id, chats_ok = fetch_project_id(session_cookie, client_revision, model, timeout)
        if not project_id and old_project_id:
            project_id = old_project_id
        elif not project_id and chats_ok:
            log("??", "???? ID")
            try:
                project_id = create_project_via_stackblitz(sess, client_revision, model, timeout)
            except Exception as exc:
                log_warn(f"???????{clip_text(exc)}")

        persist_success(account_name, email, password, session_cookie, project_id, client_revision, model)
        log("??", f"session ??? | project_id={project_id or '-'} | token-stats={stats_status}")

        return {
            "success": True,
            "name": account_name,
            "email": email,
            "project_id": project_id or "",
            "token_stats": stats_payload.get("tokenStats") if isinstance(stats_payload, dict) else None,
        }
    except Exception as exc:
        log_err(clip_text(exc, 500))
        return {
            "success": False,
            "name": record.get("name") or args.account or "",
            "email": record.get("email") or args.email or "",
            "reason": clip_text(exc, 500),
        }
    finally:
        log_prefix.reset(token)


async def refresh_batch(args: argparse.Namespace, targets: List[Dict[str, Any]]) -> None:
    success = 0
    failure = 0
    semaphore = asyncio.Semaphore(max(1, int(args.concurrency)))

    async def runner(index: int, item: Dict[str, Any]) -> Dict[str, Any]:
        label = f"R{index:02d}"
        async with semaphore:
            return await asyncio.to_thread(refresh_one_sync, args, item, label)

    tasks = [asyncio.create_task(runner(index, item)) for index, item in enumerate(targets, start=1)]
    for task in asyncio.as_completed(tasks):
        result = await task
        if result.get("success"):
            success += 1
            log("??", f"?? {success}/{len(targets)} | ??={result.get('name')} | project_id={result.get('project_id') or '-'}")
        else:
            failure += 1
            log_warn(f"?? {failure} | ??={result.get('name') or '-'} | ??={result.get('reason') or 'unknown'}")

    log("??", f"?????={success} ??={failure} ??={len(targets)}")


async def main() -> None:
    args = parse_args()
    payload = load_accounts_payload()
    accounts = payload.get("accounts") or []
    targets = pick_targets(args)

    log("??", f"???={len(accounts)} auth??={count_auth_bad_accounts(accounts)} ??={max(1, int(args.concurrency))} ??={max(5, int(args.request_timeout))}s")

    if not targets:
        log_warn("???????????")
        return

    log("??", f"????={len(targets)}")

    if len(targets) == 1:
        refresh_one_sync(args, targets[0], "R01")
        return

    await refresh_batch(args, targets)


if __name__ == "__main__":
    asyncio.run(main())
