import argparse
import os
import secrets
import time

try:
    from patchright.sync_api import sync_playwright
except ImportError:  # fallback to playwright
    from playwright.sync_api import sync_playwright


def env(name, default=""):
    val = os.getenv(name, default)
    return val.strip() if isinstance(val, str) else val


def wait_for_turnstile_token(page, timeout_sec=180):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            token = page.eval_on_selector(
                'input[name="cf-turnstile-response"]',
                "el => el && el.value ? el.value : ''",
            )
        except Exception:
            token = ""
        if token and len(token) > 100:
            return token
        time.sleep(0.5)
    return ""


def try_click_turnstile(page):
    selectors = [
        'iframe[src*="challenges.cloudflare.com"]',
        'iframe[title*="Cloudflare" i]',
        'iframe[title*="challenge" i]',
        'div[class*="turnstile" i]',
        'div[id*="turnstile" i]',
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0:
                loc.scroll_into_view_if_needed(timeout=2000)
                loc.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="https://stackblitz.com/register",
        help="Register page URL",
    )
    parser.add_argument("--headless", action="store_true", help="Run headless.")
    parser.add_argument("--timeout", type=int, default=240, help="Timeout seconds.")
    parser.add_argument("--email", default=env("E2E_EMAIL"))
    parser.add_argument("--username", default=env("E2E_USERNAME"))
    parser.add_argument("--password", default=env("E2E_PASSWORD"))
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Actually submit the registration (default is dry run).",
    )
    args = parser.parse_args()

    if not args.email or not args.password:
        raise SystemExit("Missing --email/--password (or E2E_EMAIL/E2E_PASSWORD).")

    username = args.username or ("u" + secrets.token_hex(6))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context()
        page = context.new_page()

        # Log Turnstile-related postMessage events in the real browser session.
        page.add_init_script(
            """
(() => {
  function redact(obj) {
    try {
      return JSON.parse(JSON.stringify(obj, (k, v) => {
        if (k === "token" || k === "cData" || k === "chlPageData" || k === "rcV") {
          return "[redacted]";
        }
        if (typeof v === "string" && v.length > 200) {
          return v.slice(0, 16) + "..." + v.slice(-8);
        }
        return v;
      }));
    } catch (e) {
      return "[unserializable]";
    }
  }
  window.addEventListener("message", (e) => {
    const payload = {
      origin: e.origin,
      data: redact(e.data),
    };
    console.info("[tsl-msg] " + JSON.stringify(payload));
  }, false);
})();
            """
        )

        def on_console(msg):
            try:
                text = msg.text()
            except Exception:
                return
            if text.startswith("[tsl-msg] "):
                print(text)

        page.on("console", on_console)

        def on_request(req):
            url = req.url
            if "challenges.cloudflare.com/cdn-cgi/challenge-platform" in url:
                print("[tsl-req]", req.method, url)

        page.on("request", on_request)

        page.goto(args.url, wait_until="domcontentloaded")
        page.fill('input[name="email"]', args.email)
        page.fill('input[name="username"]', username)
        page.fill('input[name="password"]', args.password)
        page.fill('input[name="password-confirm"]', args.password)

        clicked = try_click_turnstile(page)
        if clicked:
            print("Clicked Turnstile widget (if visible).")

        print("Complete Turnstile in the browser window...")
        token = wait_for_turnstile_token(page, timeout_sec=args.timeout)
        if not token:
            print("No Turnstile token detected within timeout.")
            browser.close()
            return
        print("Turnstile token detected:",
              token[:16] + "..." + token[-8:], "len=", len(token))

        if not args.submit:
            print("Dry run mode. Add --submit to actually register.")
            browser.close()
            return

        # submit
        try:
            page.get_by_role("button", name="Sign Up").click()
        except Exception:
            page.locator('button[type="submit"]').first.click()

        def is_reg_resp(resp):
            return (
                "/api/users/registrations" in resp.url
                and resp.request.method == "POST"
            )

        resp = page.wait_for_response(is_reg_resp, timeout=args.timeout * 1000)
        status = resp.status
        body = ""
        try:
            body = resp.text()[:400]
        except Exception:
            pass

        print("register status:", status)
        if body:
            print("register response:", body)

        browser.close()


if __name__ == "__main__":
    main()
