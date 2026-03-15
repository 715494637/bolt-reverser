# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "pydoll-python",
#     "requests",
# ]
# ///

import argparse
import asyncio
import json
import os
import secrets
import time
from datetime import datetime
import threading
import urllib.parse
import urllib.request
import inspect
import requests
from pathlib import Path
from contextvars import ContextVar

try:
    from imap_2925 import wait_for_confirm_link
except Exception:
    wait_for_confirm_link = None

from pydoll.browser import Chrome
from pydoll.browser.options import ChromiumOptions
from pydoll.exceptions import PageLoadTimeout
from pydoll.protocol.network.types import ResourceType

LOG_PREFIX = ContextVar("log_prefix", default="")
ACCOUNT_FILE_LOCK = threading.Lock()


def load_local_env(path=".env"):
    env_path = Path(path)
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
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


def env(name, default=""):
    val = os.getenv(name, default)
    return val.strip() if isinstance(val, str) else val


def env_int(name, default):
    raw = env(name, str(default))
    try:
        return int(raw)
    except Exception:
        return default


def log(step, msg=""):
    prefix = LOG_PREFIX.get("")
    if prefix:
        step = f"{prefix}/{step}"
    if msg:
        print(f"[{step}] {msg}")
    else:
        print(f"[{step}]")


def log_warn(msg):
    log("提示", msg)


def log_err(msg):
    log("错误", msg)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="https://stackblitz.com/register")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--email", default=env("E2E_EMAIL"))
    p.add_argument("--username", default=env("E2E_USERNAME"))
    p.add_argument("--password", default=env("E2E_PASSWORD"))
    p.add_argument("--submit", action="store_true", default=None, help="Force submit (default on).")
    p.add_argument("--no-submit", action="store_true", help="Dry run only.")
    p.add_argument("--har", default="reverse-records/turnstile_flow.har")
    p.add_argument("--har-dir", default=env("BOLT_REGISTER_HAR_DIR", "reverse-records/register-batch"))
    p.add_argument("--no-sandbox", action="store_true")
    p.add_argument("--wait-seconds", type=int, default=0)
    p.add_argument("--nav-timeout", type=int, default=30, help="Navigation timeout seconds.")
    p.add_argument("--token-timeout", type=int, default=180, help="Wait for Turnstile token seconds.")
    p.add_argument("--mail-timeout", type=int, default=env_int("BOLT_REGISTER_MAIL_TIMEOUT", 120))
    p.add_argument("--mail-poll-seconds", type=int, default=env_int("BOLT_REGISTER_MAIL_POLL_SECONDS", 5))
    p.add_argument("--target-count", type=int, default=env_int("BOLT_REGISTER_TARGET_COUNT", 20))
    p.add_argument("--concurrency", type=int, default=env_int("BOLT_REGISTER_CONCURRENCY", 3))
    return p.parse_args()


def generate_alias_email(base_email):
    if "@" not in base_email:
        raise ValueError("Invalid base email")
    user, domain = base_email.split("@", 1)
    suffix = secrets.token_hex(3)
    return f"{user}_{suffix}@{domain}"


def generate_password():
    # Simple strong-ish password: letters + digits + symbol
    return "P@" + secrets.token_hex(8)


def find_token_in_har(har_path):
    try:
        data = json.loads(Path(har_path).read_text(encoding="utf-8"))
    except Exception:
        return "", ""

    entries = data.get("log", {}).get("entries", [])
    for entry in entries:
        req = entry.get("request", {})
        url = req.get("url", "")
        if "/api/users/registrations" not in url:
            continue
        post = req.get("postData", {})
        text = post.get("text", "") or ""
        try:
            body = json.loads(text)
        except Exception:
            body = {}
        token = body.get("captcha_verification_token") or body.get("cf-turnstile-response") or ""
        return token, text[:400]
    return "", ""


def find_registration_result(har_path):
    try:
        data = json.loads(Path(har_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    entries = data.get("log", {}).get("entries", [])
    for entry in reversed(entries):
        req = entry.get("request", {})
        url = req.get("url", "")
        if "/api/users/registrations" not in url:
            continue
        resp = entry.get("response", {}) or {}
        status = resp.get("status")
        content = (resp.get("content") or {})
        text = content.get("text") or ""
        encoding = content.get("encoding")
        if isinstance(text, str) and encoding == "base64":
            try:
                import base64
                text = base64.b64decode(text).decode("utf-8", errors="replace")
            except Exception:
                pass
        return {
            "status": status,
            "body_preview": (text[:400] if isinstance(text, str) else ""),
        }
    return None


def find_registration_request(har_path):
    try:
        data = json.loads(Path(har_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    entries = data.get("log", {}).get("entries", [])
    for entry in reversed(entries):
        req = entry.get("request", {})
        url = req.get("url", "")
        if "/api/users/registrations" not in url:
            continue
        post = req.get("postData", {}) or {}
        text = post.get("text", "") or ""
        return text[:600] if isinstance(text, str) else ""
    return None


async def wait_query(tab, selector, timeout_sec=20):
    deadline = time.time() + timeout_sec
    last_err = None
    while time.time() < deadline:
        try:
            el = await tab.query(selector)
            if el:
                try:
                    if await el.is_interactable():
                        return el
                except Exception:
                    return el
        except Exception as e:
            last_err = e
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Element not ready: {selector}. Last error: {last_err}")


async def query_exists(tab, selector):
    try:
        return bool(await tab.query(selector))
    except Exception:
        return False


async def wait_any_selector(tab, selectors, timeout_sec=20):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        for selector in selectors:
            if await query_exists(tab, selector):
                return selector
        await asyncio.sleep(0.2)
    return ""


def extract_redirect_to(confirm_link):
    try:
        u = urllib.parse.urlparse(confirm_link)
        qs = urllib.parse.parse_qs(u.query, keep_blank_values=True)
        redirect_to = qs.get("redirect_to", [None])[0]
        if not redirect_to:
            return ""
        redirect_to = urllib.parse.unquote(redirect_to)
        if redirect_to.startswith("http"):
            return redirect_to
        if not redirect_to.startswith("/"):
            redirect_to = "/" + redirect_to
        return "https://stackblitz.com" + redirect_to
    except Exception:
        return ""


async def safe_type(el, text):
    try:
        await el.type_text(text, humanize=True)
        return True
    except Exception:
        pass
    try:
        await el.click()
        await el.type_text(text, humanize=True)
        return True
    except Exception:
        pass
    try:
        await el.insert_text(text)
        return True
    except Exception:
        return False


async def tab_eval(tab, script):
    for name in ("evaluate", "eval", "run_js", "execute_script"):
        fn = getattr(tab, name, None)
        if fn:
            try:
                raw = await fn(script)
                # unwrap common CDP-style results
                if isinstance(raw, dict):
                    inner = raw.get("result")
                    if isinstance(inner, dict) and "result" in inner and isinstance(inner["result"], dict):
                        inner = inner["result"]
                    if isinstance(inner, dict):
                        if "value" in inner:
                            return inner["value"]
                        if inner.get("type") == "string" and "description" in inner:
                            return inner.get("description")
                return raw
            except Exception:
                continue
    return None


async def get_input_value(el):
    try:
        val = await el.value
        if isinstance(val, str):
            return val
    except Exception:
        pass
    for name in ("get_attribute", "get_attr", "getAttribute"):
        fn = getattr(el, name, None)
        if fn:
            try:
                val = await fn("value")
                if isinstance(val, str):
                    return val
            except Exception:
                continue
    return ""


async def get_turnstile_token(tab):
    # Prefer DOM access via JS (covers input/textarea and shadow-inserted fields).
    js_val = await tab_eval(
        tab,
        """
(() => {
  const sel = 'input[name="cf-turnstile-response"],textarea[name="cf-turnstile-response"]';
  const el = document.querySelector(sel);
  if (el && el.value) return el.value;
  return '';
})()
""",
    )
    if isinstance(js_val, str) and len(js_val) > 100:
        return js_val

    try:
        el = await tab.query('input[name="cf-turnstile-response"]')
        if not el:
            el = await tab.query('textarea[name="cf-turnstile-response"]')
        if not el:
            return ""
        val = await get_input_value(el)
        if isinstance(val, str) and len(val) > 100:
            return val
    except Exception:
        pass
    return ""


async def wait_for_turnstile_token(tab, timeout_sec=180):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        token = await get_turnstile_token(tab)
        if token:
            return token
        await asyncio.sleep(0.5)
    return ""


async def set_value_with_events(tab, selector, value):
    js = (
        "(() => {"
        "const el = document.querySelector(%s);"
        "if (!el) return false;"
        "el.focus();"
        "el.value = %s;"
        "el.dispatchEvent(new Event('input', {bubbles: true}));"
        "el.dispatchEvent(new Event('change', {bubbles: true}));"
        "return true;"
        "})()"
    ) % (json.dumps(selector), json.dumps(value))
    return await tab_eval(tab, js)


async def dump_submit_state(tab):
    js = """
(() => {
  const tokenEl = document.querySelector('input[name="cf-turnstile-response"],textarea[name="cf-turnstile-response"]');
  const tokenLen = tokenEl && tokenEl.value ? tokenEl.value.length : 0;
  const form = document.querySelector('form');
  const formValid = form ? form.checkValidity() : null;
  const buttons = Array.from(document.querySelectorAll('button')).map(b => ({
    text: (b.textContent || '').trim().slice(0, 40),
    type: b.getAttribute('type'),
    disabled: !!b.disabled,
    ariaDisabled: b.getAttribute('aria-disabled'),
    id: b.id || null,
    className: b.className || null,
  }));
  const checkboxes = Array.from(document.querySelectorAll('input[type="checkbox"]')).map(c => ({
    name: c.name || null,
    id: c.id || null,
    checked: !!c.checked,
    required: !!c.required,
  }));
  return JSON.stringify({ tokenLen, formValid, buttons, checkboxes });
})()
"""
    try:
        state_raw = await tab_eval(tab, js)
        if isinstance(state_raw, str) and state_raw.strip():
            try:
                state = json.loads(state_raw)
            except Exception:
                state = state_raw
            log("状态", f"提交状态：{state}")
        else:
            log("状态", "提交状态：<unavailable>")
    except Exception as e:
        log_warn(f"提交状态读取失败：{e}")


async def check_required_checkboxes(tab):
    js = """
(() => {
  const req = Array.from(document.querySelectorAll('input[type="checkbox"][required]'));
  if (!req.length) return 0;
  let changed = 0;
  for (const c of req) {
    if (!c.checked) {
      c.click();
      c.dispatchEvent(new Event('change', {bubbles: true}));
      changed++;
    }
  }
  return changed;
})()
"""
    val = await tab_eval(tab, js)
    if isinstance(val, int) and val > 0:
        log("勾选", f"已勾选 {val} 个必选项")


async def wait_for_button_enabled(tab, timeout_sec=20):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        state = await tab_eval(
            tab,
            "(() => { const b = Array.from(document.querySelectorAll('button')).find(x => /sign\\s*up/i.test(x.textContent||'')); return b ? (!b.disabled) : null; })()",
        )
        if state is True:
            return True
        await asyncio.sleep(0.5)
    return False


async def wait_for_url_contains(tab, needle, timeout_sec=60):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        url = await tab_eval(tab, "location.href")
        if isinstance(url, str) and needle in url:
            return url
        await asyncio.sleep(0.5)
    return ""


async def best_effort_go_to(tab, url, timeout_sec=10, step="页面跳转"):
    try:
        await tab.go_to(url, timeout=timeout_sec)
    except PageLoadTimeout:
        log_warn(f"{step}超时，继续等待页面元素")
    except Exception as e:
        log_warn(f"{step}异常：{repr(e)}")


async def ensure_register_page(tab, register_url, timeout_sec=15):
    form_selectors = [
        'input[name="email"]',
        'input[name="username"]',
        'input[name="password"]',
    ]
    cur = await tab_eval(tab, "location.href")
    if isinstance(cur, str) and "stackblitz.com/register" in cur:
        return True
    if await wait_any_selector(tab, form_selectors, timeout_sec=1):
        return True
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        cur = await tab_eval(tab, "location.href")
        if isinstance(cur, str) and "stackblitz.com/register" in cur:
            return True
        if await wait_any_selector(tab, form_selectors, timeout_sec=0.5):
            return True
        await asyncio.sleep(0.2)
    asyncio.create_task(best_effort_go_to(tab, register_url, timeout_sec=8, step="注册页补跳转"))
    if await wait_any_selector(tab, form_selectors, timeout_sec=8):
        return True
    return False


async def finish_bolt_oauth(tab):
    js = """
async () => {
  const r = await fetch('https://bolt.new/api/sessions', {
    method: 'POST',
    headers: {'content-type':'application/json'},
    body: JSON.stringify({finish: true, upgrade: false})
  });
  return await r.text();
}
"""
    return await tab_eval(tab, f"({js})()")


def build_register_url(authorize_uri):
    u = urllib.parse.urlparse(authorize_uri)
    redirect_to = u.path + (f"?{u.query}" if u.query else "")
    return "https://stackblitz.com/register?redirect_to=" + urllib.parse.quote(redirect_to, safe="")


def parse_set_cookie(header_value):
    if not header_value:
        return "", ""
    parts = header_value.split(";", 1)
    name_value = parts[0].strip()
    if "=" not in name_value:
        return "", ""
    name, value = name_value.split("=", 1)
    return name.strip(), value.strip()


def find_cookie_in_har(har_path, cookie_name, domain_hint=None):
    try:
        data = json.loads(Path(har_path).read_text(encoding="utf-8"))
    except Exception:
        return ""
    entries = data.get("log", {}).get("entries", [])
    for entry in entries:
        url = (entry.get("request") or {}).get("url", "")
        if domain_hint and domain_hint not in url:
            continue
        headers = (entry.get("response") or {}).get("headers", []) or []
        for h in headers:
            if str(h.get("name", "")).lower() != "set-cookie":
                continue
            name, value = parse_set_cookie(h.get("value", ""))
            if name == cookie_name and value:
                return value
    return ""


def fetch_project_id(cookie_value):
    if not cookie_value:
        return "", False
    req = urllib.request.Request(
        "https://bolt.new/api/chats",
        headers={
            "accept": "application/json, text/plain, */*",
            "cookie": f"__session={cookie_value}",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = getattr(resp, "status", 200)
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return "", False
    chats = data.get("chats") or []
    if not chats:
        return "", True
    return chats[0].get("projectId") or "", True


def ensure_project_id_via_requests(session_cookie, bolt_client_revision="f05eb54", model="claude-sonnet-4-5-20250929"):
    sess = requests.Session()
    sess.cookies.set("__session", session_cookie, domain="bolt.new", path="/")
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://bolt.new",
        "referer": "https://bolt.new/",
        "x-bolt-client-revision": bolt_client_revision,
        "x-bolt-selected-model": model,
    }
    r = sess.get("https://bolt.new/api/chats", headers=headers, timeout=30)
    if r.ok:
        try:
            data = r.json()
            chats = data.get("chats") or []
            if chats:
                return chats[0].get("projectId") or "", True, r.status_code, ""
        except Exception:
            pass

    # Create a project via the official create-empty-project endpoint.
    try:
        resp = sess.post(
            "https://bolt.new/api/projects/sb1/fork",
            headers=headers,
            json={"project": {"appFiles": {}}},
            timeout=30,
        )
        if resp.ok:
            try:
                data = resp.json()
                pid = data.get("id") or data.get("projectId") or ""
                if pid:
                    return str(pid), True, resp.status_code, ""
            except Exception:
                pass
    except Exception:
        pass

    # Fallback: try creating via a minimal chat request (less reliable).
    msg_id = secrets.token_urlsafe(12)
    payload = {
        "messages": [
            {
                "id": msg_id,
                "role": "user",
                "content": "Hello",
                "rawContent": "Hello",
                "cache": False,
                "parts": [],
            }
        ],
        "isFirstPrompt": True,
        "featurePreviews": {"reasoning": False, "diffs": False, "imageGeneration": False},
        "errorReasoning": None,
        "framework": "vite-react",
        "promptMode": "build",
        "selectedModel": model,
        "stripeStatus": "not-configured",
        "usesInspectedElement": False,
        "runningCommands": [],
        "projectFiles": {
            "visible": [],
            "hidden": [
                "/home/project/.bolt/prompt",
                "/home/project/.bolt/config.json",
            ],
        },
        "globalSystemPrompt": "",
        "projectPrompt": "",
        "dependencies": [],
        "hostingProvider": "bolt",
        "problems": "",
        "id": secrets.token_urlsafe(8),
        "streamProtocol": "data",
        "headers": {},
        "requestType": "generate",
    }
    try:
        resp = sess.post("https://bolt.new/api/chat/v2", headers=headers, json=payload, stream=True, timeout=30)
        # Read a few lines then close to avoid long streaming waits.
        for idx, _ in enumerate(resp.iter_lines()):
            if idx >= 5:
                break
    finally:
        try:
            resp.close()
        except Exception:
            pass

    # Re-check chats
    r = sess.get("https://bolt.new/api/chats", headers=headers, timeout=30)
    if r.ok:
        try:
            data = r.json()
            chats = data.get("chats") or []
            if chats:
                return chats[0].get("projectId") or "", True, r.status_code, ""
        except Exception:
            pass
    return "", False, r.status_code if r is not None else None, (r.text[:200] if r is not None else "")


def save_account(email, cookie_value, project_id, password="", account_name=""):
    path = Path("bolt_accounts.json")
    with ACCOUNT_FILE_LOCK:
        payload = {"accounts": [], "rotation": "round_robin"}
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {"accounts": [], "rotation": "round_robin"}
        accounts = payload.get("accounts") or []
        name = account_name or f"account-{email.split('@')[0]}"
        cookie = f"__session={cookie_value}"
        target = None
        for item in accounts:
            if item.get("name") == name or item.get("cookie") == cookie or item.get("email") == email:
                target = item
                break
        if target is None:
            target = {
                "name": name,
                "sender": {"userId": "", "username": "", "name": "", "avatar": ""},
                "bolt_client_revision": "f05eb54",
                "default_model": "claude-sonnet-4-5-20250929",
                "max_concurrency": 2,
            }
            accounts.append(target)
        target["name"] = name
        target["email"] = email
        if password:
            target["password"] = password
        target["cookie"] = cookie
        if project_id:
            target["project_id"] = project_id
        else:
            target.setdefault("project_id", "")
        target.setdefault("sender", {"userId": "", "username": "", "name": "", "avatar": ""})
        target.setdefault("bolt_client_revision", "f05eb54")
        target.setdefault("default_model", "claude-sonnet-4-5-20250929")
        target.setdefault("max_concurrency", 2)
        quota = target.get("quota") or {}
        if "auth_disabled_until" in quota:
            quota["auth_disabled_until"] = None
        target["quota"] = quota
        payload["accounts"] = accounts
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return True


def _parse_iso_ts(value: str) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return 0.0


def is_ok_account(item: dict) -> bool:
    cookie = (item.get("cookie") or "").strip()
    project_id = (item.get("project_id") or "").strip()
    return bool(cookie and project_id)


def is_available_account(item: dict, now_ts: float) -> bool:
    if not is_ok_account(item):
        return False
    quota = item.get("quota") or {}
    if quota.get("monthly_exhausted"):
        return False
    if _parse_iso_ts(quota.get("auth_disabled_until")) > now_ts:
        return False
    if _parse_iso_ts(quota.get("daily_disabled_until")) > now_ts:
        return False
    if _parse_iso_ts(quota.get("cooldown_until")) > now_ts:
        return False
    return True


def get_account_stats():
    path = Path("bolt_accounts.json")
    with ACCOUNT_FILE_LOCK:
        if not path.exists():
            return 0, 0, 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return 0, 0, 0
        accounts = payload.get("accounts") or []
        total = len(accounts)
        ok_count = sum(1 for item in accounts if is_ok_account(item))
        now_ts = time.time()
        available = sum(1 for item in accounts if is_available_account(item, now_ts))
        return total, ok_count, available


async def start_bolt_session(tab, destination="https://bolt.new/", prefer_request_api=True):
    rid = secrets.token_urlsafe(12)
    payload = {"start": {"destination": destination, "rid": rid}, "upgrade": False, "ssoFlow": False}
    header_list = [
        {"name": "content-type", "value": "application/json"},
        {"name": "accept", "value": "application/json, text/plain, */*"},
    ]
    # Prefer using the browser request API to avoid loading bolt.new UI (only if allowed).
    if prefer_request_api:
        req = getattr(tab, "request", None)
        if req and hasattr(req, "post"):
            try:
                log("会话", "尝试通过浏览器请求 API 获取 authorizeUri")
                if hasattr(req, "enable"):
                    try:
                        maybe = req.enable()
                        if inspect.isawaitable(maybe):
                            await maybe
                    except Exception as e:
                        log_warn(f"request.enable 失败：{repr(e)}")

                header_tuples = [
                    ("content-type", "application/json"),
                    ("accept", "application/json, text/plain, */*"),
                ]
                attempts = [
                    ("json+headers", dict(json=payload, headers=header_list)),
                    ("json+tuple-headers", dict(json=payload, headers=header_tuples)),
                    ("json", dict(json=payload)),
                    ("data+headers", dict(data=json.dumps(payload), headers=header_list)),
                    ("data+tuple-headers", dict(data=json.dumps(payload), headers=header_tuples)),
                    ("data", dict(data=json.dumps(payload))),
                ]
                for label, kwargs in attempts:
                    try:
                        resp = req.request("POST", "https://bolt.new/api/sessions", **kwargs)
                        if inspect.isawaitable(resp):
                            resp = await resp
                        data = await read_response_json(resp)
                        if isinstance(data, dict) and data.get("authorizeUri"):
                            log("会话", "已获取 authorizeUri（浏览器请求）")
                            return data.get("authorizeUri") or ""
                        raw_text = await safe_response_text(resp)
                        if raw_text:
                            log_warn(f"authorizeUri 为空，响应预览：{raw_text[:200]}")
                    except Exception as e:
                        log_warn(f"浏览器请求失败：{repr(e)}")
            except Exception as e:
                log_warn(f"请求 API 失败，改用页面 fetch：{repr(e)}")

    # Fallback: load bolt.new and run fetch in-page (required for same-origin due to CORS)
    log("会话", "加载 bolt.new 获取 authorizeUri")
    try:
        await tab.go_to("https://bolt.new/", timeout=10)
    except PageLoadTimeout:
        log_warn("bolt.new 加载超时，继续执行页面 fetch")
    js = """
async (payload) => {
  const r = await fetch('https://bolt.new/api/sessions', {
    method: 'POST',
    headers: {'content-type':'application/json'},
    body: JSON.stringify(payload)
  });
  return await r.text();
}
"""
    text = await tab_eval(tab, f"({js})({json.dumps(payload)})")
    try:
        data = json.loads(text) if isinstance(text, str) else text
    except Exception:
        data = {}
    return data.get("authorizeUri") or ""


def start_bolt_session_non_browser(destination="https://bolt.new/"):
    rid = secrets.token_urlsafe(12)
    payload = {"start": {"destination": destination, "rid": rid}, "upgrade": False, "ssoFlow": False}
    sess = requests.Session()
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/plain, */*",
        "origin": "https://bolt.new",
        "referer": "https://bolt.new/",
        "user-agent": "Mozilla/5.0",
    }
    resp = sess.post("https://bolt.new/api/sessions", json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    oauth = (
        sess.cookies.get("__oauth", domain="bolt.new")
        or sess.cookies.get("__oauth")
        or resp.cookies.get("__oauth")
    )
    return data.get("authorizeUri") or "", oauth


async def set_bolt_oauth_cookie(tab, value):
    if not value:
        return False
    try:
        await tab.set_cookies(
            [
                {
                    "name": "__oauth",
                    "value": value,
                    "url": "https://bolt.new",
                    "httpOnly": True,
                    "secure": True,
                }
            ]
        )
        return True
    except Exception:
        return False


async def read_response_json(resp):
    if resp is None:
        return {}
    if isinstance(resp, (dict, list)):
        return resp
    if isinstance(resp, str):
        try:
            return json.loads(resp)
        except Exception:
            return {}
    fn = getattr(resp, "json", None)
    if callable(fn):
        try:
            return await fn()
        except TypeError:
            try:
                return fn()
            except Exception:
                pass
        except Exception:
            pass
    for attr in ("text", "body", "content"):
        val = getattr(resp, attr, None)
        if val is None:
            continue
        try:
            val = await val() if callable(val) else val
        except Exception:
            val = val() if callable(val) else val
        if isinstance(val, (bytes, bytearray)):
            try:
                val = val.decode("utf-8", errors="replace")
            except Exception:
                pass
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                continue
    return {}


async def safe_response_text(resp):
    if resp is None:
        return ""
    for attr in ("text", "body", "content"):
        val = getattr(resp, attr, None)
        if val is None:
            continue
        try:
            val = await val() if callable(val) else val
        except Exception:
            try:
                val = val() if callable(val) else val
            except Exception:
                continue
        if isinstance(val, (bytes, bytearray)):
            try:
                val = val.decode("utf-8", errors="replace")
            except Exception:
                pass
        if isinstance(val, str):
            return val
    return ""


async def create_fresh_tab(browser):
    # Prefer an isolated context to avoid reusing StackBlitz/Bolt cookies.
    if hasattr(browser, "create_browser_context") and hasattr(browser, "new_tab"):
        try:
            ctx = await browser.create_browser_context()
            return await browser.new_tab(browser_context_id=ctx)
        except Exception:
            pass
    return await browser.start()


async def create_context_tabs(browser):
    # Create a shared browser context so background and main tabs share cookies.
    if hasattr(browser, "create_browser_context") and hasattr(browser, "new_tab"):
        try:
            ctx = await browser.create_browser_context()
            bg = await browser.new_tab(browser_context_id=ctx)
            main = await browser.new_tab(browser_context_id=ctx)
            return main, bg
        except Exception:
            pass
    main = await browser.start()
    return main, None


async def click_submit(tab):
    selectors = [
        'button[type="submit"]',
        'button[data-testid="signup-submit"]',
        'button[data-test="signup-submit"]',
    ]
    for sel in selectors:
        try:
            btn = await tab.query(sel)
            if btn:
                await tab_eval(
                    tab,
                    f"(() => {{ const el = document.querySelector({json.dumps(sel)}); if (el) {{ el.scrollIntoView({{block:'center'}}); }} }})()",
                )
                await btn.click()
                return True
        except Exception:
            continue
    try:
        btn = await tab.find(tag_name="button", text="Sign Up", raise_exc=False)
        if btn:
            await tab_eval(
                tab,
                "(() => { const b = Array.from(document.querySelectorAll('button')).find(x => /sign\\s*up/i.test(x.textContent||'')); if (b) b.scrollIntoView({block:'center'}); })()",
            )
            await btn.click()
            return True
    except Exception:
        pass
    try:
        js_ok = await tab_eval(
            tab,
            "(() => { const b = Array.from(document.querySelectorAll('button')).find(x => /sign\\s*up/i.test(x.textContent||'')); if (b) b.click(); })()",
        )
        return bool(js_ok is None or js_ok is True)
    except Exception:
        return False


async def get_cookie_value(tab, name, domain_contains=None):
    try:
        cookies = await tab.get_cookies()
    except Exception:
        return ""
    for c in cookies or []:
        if c.get("name") != name:
            continue
        if domain_contains and domain_contains not in (c.get("domain") or ""):
            continue
        return c.get("value") or ""
    return ""


async def get_cookies_for_domain(tab, domain_contains):
    try:
        cookies = await tab.get_cookies()
    except Exception:
        return []
    out = []
    for c in cookies or []:
        if domain_contains and domain_contains not in (c.get("domain") or ""):
            continue
        out.append(c)
    return out


def add_cookies_to_session(sess, cookies):
    for c in cookies or []:
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain") or ""
        path = c.get("path") or "/"
        if not name or value is None:
            continue
        try:
            sess.cookies.set(name, value, domain=domain, path=path)
        except Exception:
            try:
                sess.cookies.set(name, value)
            except Exception:
                pass


def complete_oauth_via_requests(confirm_link, oauth_cookie, sb_cookies, bolt_client_revision="f05eb54"):
    sess = requests.Session()
    # Apply StackBlitz cookies from browser (login/session)
    add_cookies_to_session(sess, sb_cookies)
    if oauth_cookie:
        sess.cookies.set("__oauth", oauth_cookie, domain="bolt.new", path="/")

    # Hit confirmation link (should redirect into oauth/authorize)
    resp = sess.get(confirm_link, allow_redirects=True, timeout=30)
    final_url = resp.url

    # If not already at authorize, try explicit authorize URL from redirect_to
    if "oauth/authorize" not in final_url:
        auth_url = extract_redirect_to(confirm_link)
        if auth_url:
            resp = sess.get(auth_url, allow_redirects=True, timeout=30)
            final_url = resp.url

    # Finish bolt oauth to mint __session
    fin = sess.post(
        "https://bolt.new/api/sessions",
        json={"finish": True, "upgrade": False},
        headers={
            "content-type": "application/json",
            "origin": "https://bolt.new",
            "referer": "https://bolt.new/",
        },
        timeout=30,
    )
    if fin.status_code >= 400:
        return "", final_url, "", False, fin.status_code, fin.text[:200]
    # Extract __session
    session_cookie = (
        sess.cookies.get("__session", domain="bolt.new")
        or sess.cookies.get("__session")
        or fin.cookies.get("__session")
    )
    # Try to create a project via StackBlitz API (same flow as UI).
    project_id = ""
    try:
        csrf = sess.cookies.get("CSRF-TOKEN", domain="stackblitz.com") or sess.cookies.get("CSRF-TOKEN")
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://bolt.new",
            "referer": "https://bolt.new/",
        }
        if csrf:
            headers["x-csrf-token"] = csrf
        resp = sess.post(
            "https://stackblitz.com/api/projects/sb1/fork",
            headers=headers,
            json={"project": {"appFiles": {}}},
            timeout=30,
        )
        if resp.ok:
            data = resp.json()
            project_id = data.get("id") or data.get("projectId") or ""
            if project_id:
                project_id = str(project_id)
    except Exception:
        pass
    chats_ok = False
    status = None
    body_preview = ""
    if session_cookie:
        r = sess.get(
            "https://bolt.new/api/chats",
            headers={
                "accept": "application/json, text/plain, */*",
                "x-bolt-client-revision": bolt_client_revision,
                "origin": "https://bolt.new",
                "referer": "https://bolt.new/",
            },
            timeout=30,
        )
        status = r.status_code
        body_preview = r.text[:200]
        if r.ok:
            chats_ok = True
            try:
                data = r.json()
                chats = data.get("chats") or []
                if chats:
                    project_id = chats[0].get("projectId") or ""
            except Exception:
                pass
    return session_cookie, final_url, project_id, chats_ok, status, body_preview


def should_submit(args):
    if args.no_submit:
        return False
    if args.submit is True:
        return True
    return True


async def register_once(args, email=None, password=None, username=None, har_path=None, worker_label=""):
    token = LOG_PREFIX.set(worker_label or "")
    session_cookie_from_browser = ""
    try:
        do_submit = should_submit(args)
        base_email = env("BASE_EMAIL", "opooo18301@2925.com")
        email = email or args.email or generate_alias_email(base_email)
        password = password or args.password or generate_password()
        username = username or args.username or ("u" + secrets.token_hex(6))
        har_path = Path(har_path or args.har)
        har_path.parent.mkdir(parents=True, exist_ok=True)

        log("准备", f"邮箱={email}")
        log("准备", f"密码={password}")

        options = ChromiumOptions()
        options.headless = bool(args.headless)
        options.add_argument("--window-size=1280,900")
        if args.no_sandbox:
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")

        async with Chrome(options=options) as browser:
            tabs_task = asyncio.create_task(create_context_tabs(browser))
            session_task = asyncio.create_task(asyncio.to_thread(start_bolt_session_non_browser))

            tab, bg_tab = await tabs_task
            if bg_tab:
                log("会话", "使用后台标签页获取授权（主标签停留注册页）")
            async with tab.request.record(
                resource_types=[ResourceType.FETCH, ResourceType.XHR, ResourceType.DOCUMENT]
            ) as capture:
                try:
                    authorize_uri = ""
                    oauth_cookie = ""
                    try:
                        authorize_uri, oauth_cookie = await session_task
                        log("会话", "已获取 authorizeUri（非浏览器）")
                    except Exception as e:
                        log_warn(f"非浏览器获取失败，改用浏览器：{repr(e)}")
                    if authorize_uri and oauth_cookie:
                        ok = await set_bolt_oauth_cookie(tab, oauth_cookie)
                        log("会话", f"注入 __oauth Cookie：{'成功' if ok else '失败'}")
                    if not authorize_uri:
                        authorize_uri = await start_bolt_session(bg_tab or tab, prefer_request_api=not bool(bg_tab))
                    register_url = args.url
                    if authorize_uri:
                        register_url = build_register_url(authorize_uri)
                    log("打开", f"注册页：{register_url}")
                    nav_task = asyncio.create_task(
                        best_effort_go_to(tab, register_url, timeout_sec=args.nav_timeout, step="打开注册页")
                    )
                    await ensure_register_page(tab, register_url, timeout_sec=15)
                    cur = await tab_eval(tab, "location.href")
                    if isinstance(cur, str):
                        log("页面", f"当前URL：{cur}")
                    email_el, user_el, pass_el, pass2_el = await asyncio.gather(
                        wait_query(tab, 'input[name="email"]', timeout_sec=30),
                        wait_query(tab, 'input[name="username"]', timeout_sec=30),
                        wait_query(tab, 'input[name="password"]', timeout_sec=30),
                        wait_query(tab, 'input[name="password-confirm"]', timeout_sec=30),
                    )

                    if nav_task.done():
                        await nav_task

                    await safe_type(email_el, email)
                    await safe_type(user_el, username)
                    await safe_type(pass_el, password)
                    await safe_type(pass2_el, password)
                    log("输入", "已填写邮箱/用户名/密码")
                    await set_value_with_events(tab, 'input[name="email"]', email)
                    await set_value_with_events(tab, 'input[name="username"]', username)
                    await set_value_with_events(tab, 'input[name="password"]', password)
                    await set_value_with_events(tab, 'input[name="password-confirm"]', password)

                    token_value = await wait_for_turnstile_token(tab, timeout_sec=args.token_timeout)
                    if token_value:
                        log("验证", "Turnstile 已通过")
                    else:
                        log_warn("Turnstile 未在超时内检测到")
                        if args.wait_seconds > 0:
                            await asyncio.sleep(args.wait_seconds)

                    if do_submit:
                        await check_required_checkboxes(tab)
                        await dump_submit_state(tab)
                        await wait_for_button_enabled(tab, timeout_sec=10)
                        ok = await click_submit(tab)
                        if ok:
                            log("提交", "已点击 Sign Up")
                        else:
                            log_warn("提交按钮点击失败，尝试备用提交")
                        if not ok:
                            await tab_eval(
                                tab,
                                "(() => { const f = document.querySelector('form'); if (!f) return false; if (f.requestSubmit) { f.requestSubmit(); return true; } f.submit(); return true; })()",
                            )
                        await asyncio.sleep(3)
                        if wait_for_confirm_link:
                            log("邮件", f"等待确认链接（超时 {args.mail_timeout}s）")
                            try:
                                link = await asyncio.wait_for(
                                    asyncio.to_thread(
                                        wait_for_confirm_link,
                                        sub_email=email,
                                        timeout_seconds=args.mail_timeout,
                                        poll_interval_seconds=args.mail_poll_seconds,
                                        link_regex=r"confirmation_token|oauth/authorize|confirmation",
                                    ),
                                    timeout=max(args.mail_timeout + 10, args.mail_timeout),
                                )
                            except asyncio.TimeoutError:
                                link = None
                            if link:
                                log("邮件", f"确认链接：{link}")
                                try:
                                    sb_cookies = await get_cookies_for_domain(tab, "stackblitz.com")
                                    session_cookie, final_url, project_id, chats_ok, status, body_preview = complete_oauth_via_requests(
                                        link, oauth_cookie, sb_cookies
                                    )
                                    if session_cookie:
                                        log("OAuth", f"请求完成：{final_url}")
                                        if chats_ok:
                                            log("校验", f"/api/chats OK status={status}")
                                        else:
                                            log_warn(f"/api/chats 失败 status={status} body={body_preview}")
                                        if not project_id:
                                            log("项目", "未找到 project_id，尝试 API 创建")
                                            pid, ok, pstatus, pbody = ensure_project_id_via_requests(session_cookie)
                                            if ok and pid:
                                                project_id = pid
                                                log("项目", f"已创建 project_id={project_id}")
                                            else:
                                                log_warn(f"项目创建失败 status={pstatus} body={pbody}")
                                        saved = save_account(email, session_cookie, project_id, password=password)
                                        if saved:
                                            log("保存", "账号已写入 bolt_accounts.json")
                                        else:
                                            log_warn("账号已存在，跳过写入")
                                        if project_id:
                                            log("项目", f"project_id={project_id}")
                                        else:
                                            log_warn("仍未获取 project_id（可手动打开一次 bolt.new）")
                                        return {
                                            "success": True,
                                            "email": email,
                                            "password": password,
                                            "project_id": project_id,
                                            "saved": saved,
                                        }
                                    log_warn("请求 OAuth 未拿到 __session，改用浏览器流程")
                                except Exception as e:
                                    log_err(f"请求 OAuth 失败：{repr(e)}")

                                await best_effort_go_to(tab, link, timeout_sec=10, step="打开确认页")
                                auth_url = extract_redirect_to(link)
                                if auth_url:
                                    log("授权", f"打开授权页：{auth_url}")
                                    await best_effort_go_to(tab, auth_url, timeout_sec=10, step="打开授权页")
                                await wait_for_url_contains(tab, "bolt.new/oauth2", timeout_sec=120)
                                await asyncio.sleep(2)
                                await finish_bolt_oauth(tab)
                                await asyncio.sleep(3)
                            else:
                                log_warn("邮件确认超时，结束当前账号")
                                return {
                                    "success": False,
                                    "email": email,
                                    "password": password,
                                    "project_id": "",
                                    "saved": False,
                                    "reason": "mail confirmation timeout",
                                }
                        else:
                            log_warn("imap_2925.py 不可用，无法读取邮件")

                    await asyncio.sleep(6)
                    try:
                        session_cookie_from_browser = await get_cookie_value(tab, "__session", domain_contains="bolt.new")
                    except Exception:
                        session_cookie_from_browser = ""
                finally:
                    try:
                        capture.save(str(har_path))
                    except Exception as e:
                        log_warn(f"HAR 保存失败：{repr(e)}")

        token_value, body_preview = find_token_in_har(har_path)
        if token_value:
            log("验证", "HAR 已捕获 Turnstile token")
        else:
            log_warn(f"HAR 未捕获 token（已保存 {har_path}）")
            if body_preview:
                log("调试", f"注册请求预览：{body_preview}")

        reg = find_registration_result(har_path)
        if reg:
            log("结果", f"注册响应 status={reg.get('status')}")
            if reg.get("body_preview"):
                log("结果", f"响应预览：{reg.get('body_preview')}")
            else:
                log("结果", "响应预览：<empty>")
        elif do_submit:
            log_warn("HAR 未找到注册请求（可能未提交或被拦截）")

        if do_submit and reg and reg.get("status") and int(reg.get("status")) >= 400:
            req_preview = find_registration_request(har_path)
            if req_preview:
                try:
                    req_obj = json.loads(req_preview)
                    if isinstance(req_obj, dict) and "password" in req_obj:
                        req_obj["password"] = "***"
                    req_preview = json.dumps(req_obj, ensure_ascii=False)[:600]
                except Exception:
                    pass
                log("调试", f"请求预览（已脱敏）：{req_preview}")

        session_cookie = session_cookie_from_browser or find_cookie_in_har(
            har_path, "__session", domain_hint="bolt.new"
        )
        if session_cookie:
            project_id, ok = fetch_project_id(session_cookie)
            if ok:
                if not project_id:
                    log("项目", "未找到 project_id，尝试 API 创建")
                    pid, ok2, pstatus, pbody = ensure_project_id_via_requests(session_cookie)
                    if ok2 and pid:
                        project_id = pid
                        log("项目", f"已创建 project_id={project_id}")
                    else:
                        log_warn(f"项目创建失败 status={pstatus} body={pbody}")
                saved = save_account(email, session_cookie, project_id, password=password)
                if saved:
                    log("保存", "账号已写入 bolt_accounts.json")
                else:
                    log_warn("账号已存在，跳过写入")
                if project_id:
                    log("项目", f"project_id={project_id}")
                else:
                    log_warn("仍未获取 project_id（可手动打开一次 bolt.new）")
                return {
                    "success": True,
                    "email": email,
                    "password": password,
                    "project_id": project_id,
                    "saved": saved,
                }
            log_warn("已有 __session 但 /api/chats 失败，未保存")
            return {
                "success": False,
                "email": email,
                "password": password,
                "project_id": "",
                "saved": False,
                "reason": "/api/chats failed",
            }

        log_warn("未发现 __session（登录可能未完成）")
        return {
            "success": False,
            "email": email,
            "password": password,
            "project_id": "",
            "saved": False,
            "reason": "__session not found",
        }
    except Exception as e:
        log_err(f"流程异常：{repr(e)}")
        return {
            "success": False,
            "email": email or "",
            "password": password or "",
            "project_id": "",
            "saved": False,
            "reason": repr(e),
        }
    finally:
        LOG_PREFIX.reset(token)


async def register_batch(args):
    target_count = max(1, int(args.target_count))
    total_count, ok_count, available_count = get_account_stats()
    missing_count = max(0, target_count - available_count)
    concurrency = max(1, int(args.concurrency))
    base_email = env("BASE_EMAIL", "opooo18301@2925.com")
    har_dir = Path(args.har_dir)
    har_dir.mkdir(parents=True, exist_ok=True)

    log(
        "批量",
        f"当前可用={available_count} 当前账号(OK)={ok_count} 总账号={total_count} 目标可用={target_count} 待创建={missing_count}",
    )
    log("批量", f"并发数={concurrency} HAR目录={har_dir}")

    if missing_count <= 0:
        log("批量", "已达到目标数量，无需创建")
        return

    success_count = 0
    failure_count = 0
    launched = 0
    attempt_limit = max(missing_count * 3, concurrency)
    pending = set()

    def spawn_attempt(attempt_no):
        email = generate_alias_email(base_email)
        password = generate_password()
        username = "u" + secrets.token_hex(6)
        local = email.split("@", 1)[0]
        har_path = har_dir / f"{attempt_no:03d}_{local}.har"
        label = f"W{attempt_no:02d}"
        return asyncio.create_task(
            register_once(
                args,
                email=email,
                password=password,
                username=username,
                har_path=har_path,
                worker_label=label,
            )
        )

    while success_count < missing_count and (launched < attempt_limit or pending):
        while len(pending) < concurrency and launched < attempt_limit and success_count + len(pending) < missing_count:
            launched += 1
            pending.add(spawn_attempt(launched))

        if not pending:
            break

        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                result = task.result()
            except Exception as e:
                failure_count += 1
                log_warn(f"批量任务异常：{repr(e)}")
                continue

            if result.get("success"):
                success_count += 1
                log(
                    "批量",
                    f"成功 {success_count}/{missing_count} | 邮箱={result.get('email')} | project_id={result.get('project_id') or '-'}",
                )
            else:
                failure_count += 1
                log_warn(
                    f"失败 {failure_count} | 邮箱={result.get('email') or '-'} | 原因={result.get('reason') or 'unknown'}"
                )

    _, _, final_count = get_account_stats()
    if success_count >= missing_count:
        log("批量", f"任务完成：当前可用账号={final_count}")
    else:
        log_warn(
            f"未达到目标：当前可用账号={final_count}，成功新增={success_count}，失败={failure_count}，尝试次数={launched}/{attempt_limit}"
        )


async def main():
    args = parse_args()
    if args.email or args.username or args.password or int(args.target_count) <= 1:
        await register_once(args)
        return
    await register_batch(args)


if __name__ == "__main__":
    asyncio.run(main())
