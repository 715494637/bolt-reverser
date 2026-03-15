import argparse
import imaplib
import re
import ssl
import time
import html
from email.parser import BytesParser
from email.policy import default as default_policy


DEFAULT_HOST = "imap.2925.com"
DEFAULT_PORT = 143
DEFAULT_USE_SSL = False  # set True if the server supports IMAPS on 993
DEFAULT_USERNAME = "opooo18301@2925.com"
DEFAULT_PASSWORD = "dddd1111"
DEFAULT_MAILBOX = "INBOX"
DEFAULT_SENDER_FILTER = "hello@stackblitz.com"


def _connect_imap(host, port, use_ssl, username, password):
    ctx = ssl.create_default_context()
    if use_ssl:
        client = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
    else:
        client = imaplib.IMAP4(host, port)
        # Only attempt STARTTLS if the server advertises it
        client.capability()
        if b"STARTTLS" in getattr(client, "capabilities", []):
            try:
                client.starttls(ssl_context=ctx)
            except imaplib.IMAP4.abort:
                # Server claims STARTTLS but doesn't support it; continue in plaintext
                pass
    client.login(username, password)
    return client


def _select_mailbox(client, mailbox):
    status, data = client.select(mailbox, readonly=True)
    if status != "OK":
        raise RuntimeError(f"Failed to select mailbox: {mailbox}")
    total = 0
    if data and data[0]:
        try:
            total = int(data[0])
        except ValueError:
            total = 0
    return total


def _recent_message_ids(client, total, limit=20):
    msg_ids = []
    try:
        status, data = client.search(None, "ALL")
        if status == "OK" and data and data[0]:
            msg_ids = data[0].split()
    except imaplib.IMAP4.error:
        msg_ids = []

    if not msg_ids and total > 0:
        start = max(1, total - (limit - 1))
        msg_ids = [str(i) for i in range(start, total + 1)]
    return msg_ids[-limit:]


def _fetch_message_bytes(client, msg_id):
    for query in ("(RFC822)", "(BODY.PEEK[])"):
        status, data = client.fetch(msg_id, query)
        if status == "OK" and data:
            for part in data:
                if isinstance(part, tuple) and part[1]:
                    return part[1]
    return b""


def _extract_text_parts(msg):
    texts = []
    if msg.is_multipart():
        parts = msg.walk()
    else:
        parts = [msg]
    for part in parts:
        if part.get_content_maintype() != "text":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        texts.append(text)
    return "\n".join(texts)


def _extract_urls(text):
    urls = []
    if text:
        text = html.unescape(text)
    urls.extend(re.findall(r'href=["\'](https?://[^"\']+)', text, flags=re.I))
    urls.extend(re.findall(r"https?://[^\s\"'<>]+", text))
    # Deduplicate while preserving order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _pick_best_url(urls, link_re=None):
    if not urls:
        return None

    bad_home = {
        "https://stackblitz.com",
        "https://stackblitz.com/",
        "http://stackblitz.com",
        "http://stackblitz.com/",
    }

    if link_re:
        urls = [u for u in urls if link_re.search(u)]
        if not urls:
            return None

    # If only homepage links are present, treat as no match
    if all(u in bad_home for u in urls):
        return None

    keywords = [
        "confirm",
        "verify",
        "verification",
        "activate",
        "activation",
        "magic",
        "token",
        "signin",
        "login",
        "auth",
        "continue",
        "reset",
        "password",
        "code",
    ]

    def score(u):
        ul = u.lower()
        s = 0
        if ul.startswith("https://"):
            s += 1
        if any(k in ul for k in keywords):
            s += 5
        if re.search(r"[?&](token|code|verify|confirmation|magic|signin|login|auth)=", ul):
            s += 5
        if re.search(r"[?&][a-z0-9_]+=.{6,}", ul):
            s += 2
        if re.search(r"/[^/].+", ul):
            s += 1
        if ul in bad_home:
            s -= 5
        if ul.endswith("/") and ul.count("/") <= 2:
            s -= 2
        return s

    return max(urls, key=lambda u: (score(u), len(u)))


def _msg_matches_recipient(msg, recipient):
    recipient_l = recipient.lower()
    for header in (
        "To",
        "Cc",
        "Delivered-To",
        "X-Original-To",
        "X-Forwarded-To",
    ):
        value = msg.get(header, "")
        if recipient_l in value.lower():
            return True
    return False


def _msg_matches_filters(msg, sender_filter=None, subject_filter=None):
    if sender_filter:
        frm = msg.get("From", "")
        if sender_filter.lower() not in frm.lower():
            return False
    if subject_filter:
        subj = msg.get("Subject", "")
        if subject_filter.lower() not in subj.lower():
            return False
    return True


def wait_for_confirm_link(
    sub_email,
    host=DEFAULT_HOST,
    port=DEFAULT_PORT,
    use_ssl=DEFAULT_USE_SSL,
    username=DEFAULT_USERNAME,
    password=DEFAULT_PASSWORD,
    mailbox=DEFAULT_MAILBOX,
    timeout_seconds=180,
    poll_interval_seconds=5,
    sender_filter=DEFAULT_SENDER_FILTER,
    subject_filter=None,
    link_regex=None,
):
    """
    Poll mailbox for latest confirmation link related to sub_email.

    Returns the first matching link found (newest-first). Returns None on timeout.
    """
    deadline = time.time() + timeout_seconds if timeout_seconds else None
    link_re = re.compile(link_regex, flags=re.I) if link_regex else None

    client = _connect_imap(host, port, use_ssl, username, password)
    try:
        while True:
            try:
                total = _select_mailbox(client, mailbox)
                msg_ids = _recent_message_ids(client, total, limit=30)
                for msg_id in reversed(msg_ids):
                    raw = _fetch_message_bytes(client, msg_id)
                    if not raw:
                        continue
                    msg = BytesParser(policy=default_policy).parsebytes(raw)
                    if not _msg_matches_recipient(msg, sub_email):
                        continue
                    if not _msg_matches_filters(msg, sender_filter, subject_filter):
                        continue
                    text = _extract_text_parts(msg)
                    urls = _extract_urls(text)
                    best = _pick_best_url(urls, link_re=link_re)
                    if best:
                        return best
            except imaplib.IMAP4.abort:
                try:
                    client.logout()
                except Exception:
                    pass
                time.sleep(2)
                client = _connect_imap(host, port, use_ssl, username, password)

            if deadline and time.time() >= deadline:
                return None
            time.sleep(poll_interval_seconds)
    finally:
        try:
            client.logout()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Wait for confirmation link in 2925 IMAP.")
    parser.add_argument("sub_email", help="Alias email, e.g. opooo18301_abc@2925.com")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--use-ssl", action="store_true", default=DEFAULT_USE_SSL)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument(
        "--sender",
        default=DEFAULT_SENDER_FILTER,
        help="Filter by sender contains",
    )
    parser.add_argument("--subject", default=None, help="Filter by subject contains")
    parser.add_argument("--link-regex", default=None, help="Regex to match confirmation link")
    args = parser.parse_args()

    link = wait_for_confirm_link(
        sub_email=args.sub_email,
        host=args.host,
        port=args.port,
        use_ssl=args.use_ssl,
        username=args.username,
        password=args.password,
        timeout_seconds=args.timeout,
        poll_interval_seconds=args.interval,
        sender_filter=args.sender,
        subject_filter=args.subject,
        link_regex=args.link_regex,
    )
    if link:
        print(link)
    else:
        print("No confirmation link found before timeout.")


if __name__ == "__main__":
    main()
