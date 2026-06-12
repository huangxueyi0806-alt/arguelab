#!/usr/bin/env python3
"""
Subscription backend for ArgueLab.
Handles: subscriber management, API server for signup, and email dispatch.

Usage:
  1. Start the API server:  python server.py --serve
  2. Send daily email:      python server.py --send /path/to/briefing.md
  3. List subscribers:      python server.py --list
  4. Add subscriber:        python server.py --add email@example.com
  5. Remove subscriber:     python server.py --remove email@example.com

Configure SMTP via environment variables or a .env file.
"""

import json
import os
import sys
import smtplib
import argparse
import hashlib
import time
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs

# ── Paths ──
BASE_DIR = Path(__file__).parent

# Auto-load .env file (if python-dotenv is installed)
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass
DATA_DIR = BASE_DIR / "data"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
TEMPLATES_DIR = BASE_DIR / "templates"

DATA_DIR.mkdir(exist_ok=True)

# ── SMTP Config ──
# Set these via environment variables. Works with any SMTP provider.
# For Resend: SMTP_HOST=live.smtp.resend.com, SMTP_PORT=587, SMTP_USER=resend
# For Gmail:  SMTP_HOST=smtp.gmail.com, SMTP_PORT=587
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "ArgueLab <dispatch@arguelab.com>")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

SERVER_HOST = os.environ.get("HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("PORT", "8080"))


# ── Subscriber storage ──

def load_subscribers() -> list[dict]:
    if not SUBSCRIBERS_FILE.exists():
        return []
    return json.loads(SUBSCRIBERS_FILE.read_text())


def save_subscribers(subs: list[dict]) -> None:
    SUBSCRIBERS_FILE.write_text(json.dumps(subs, indent=2, ensure_ascii=False))


def add_subscriber(email: str, name: str = "") -> dict:
    subs = load_subscribers()
    email = email.strip().lower()
    if any(s["email"] == email for s in subs):
        return {"status": "exists", "email": email}
    sub = {
        "email": email,
        "name": name or email.split("@")[0],
        "subscribed_at": datetime.now().isoformat(),
        "verified": False,
        "token": hashlib.sha256(f"{email}{time.time()}".encode()).hexdigest()[:12],
    }
    subs.append(sub)
    save_subscribers(subs)
    return {"status": "ok", "email": email, "token": sub["token"]}


def remove_subscriber(email: str) -> dict:
    subs = load_subscribers()
    email = email.strip().lower()
    new_subs = [s for s in subs if s["email"] != email]
    if len(new_subs) == len(subs):
        return {"status": "not_found", "email": email}
    save_subscribers(new_subs)
    return {"status": "ok", "email": email}


# ── Email template builder ──

EMAIL_CSS = """
  body { margin:0; padding:0; background:#0A0D12; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif; color:#E2E5EC; }
  .container { max-width:620px; margin:0 auto; background:#11141C; }
  .header { padding:36px 32px 24px; border-bottom:1px solid rgba(136,157,196,0.1); }
  .logo { font-family:Georgia,'Times New Roman',serif; font-size:24px; font-weight:700; color:#E2E5EC; }
  .logo span { color:#889DC4; }
  .header-date { font-size:13px; color:#88909F; margin-top:6px; }
  .tagline { font-size:12px; color:#6B7280; margin:4px 0 0; font-style:italic; }
  .topic-line { font-size:14px; color:#889DC4; margin:8px 0 0; font-weight:600; }
  .section { padding:28px 32px; border-bottom:1px solid rgba(136,157,196,0.06); }
  .section:last-child { border-bottom:none; }
  .section-title { font-family:Georgia,'PingFang SC',serif; font-size:18px; font-weight:700; color:#E2E5EC; margin:0 0 16px; padding-bottom:8px; border-bottom:1px solid rgba(136,157,196,0.08); }
  .cn-body { font-size:14.5px; color:#B0B8C4; line-height:1.85; }
  .cn-body strong { color:#E2E5EC; }
  .en-body { font-size:14px; color:#C8CFDE; line-height:1.85; }
  .arg-label { display:inline-block; font-size:11px; font-weight:700; padding:2px 8px; border-radius:3px; margin:6px 0; }
  .arg-thesis { background:rgba(136,157,196,0.15); color:#889DC4; }
  .arg-premise { background:rgba(22,163,74,0.12); color:#4ade80; }
  .arg-evidence { background:rgba(217,119,6,0.12); color:#fbbf24; }
  .arg-counter { background:rgba(124,58,237,0.12); color:#a78bfa; }
  .arg-conclusion { background:rgba(219,39,119,0.12); color:#f472b6; }
  .expr-block { padding:16px; background:rgba(20,24,33,0.5); border-radius:8px; margin-bottom:12px; border-left:2px solid rgba(136,157,196,0.2); }
  .expr-word { font-family:Georgia,serif; font-size:16px; font-weight:700; color:#889DC4; }
  .expr-tag { font-size:11px; background:rgba(136,157,196,0.1); color:#889DC4; padding:2px 8px; border-radius:4px; display:inline-block; margin:4px 0; }
  .expr-example { font-size:13.5px; color:#B0B8C4; font-style:italic; margin:8px 0 0; padding:10px; background:rgba(17,20,28,0.6); border-radius:6px; }
  .chain-block { padding:14px 18px; background:rgba(20,24,33,0.5); border-radius:8px; margin-bottom:10px; }
  .quote-block { padding:16px; background:rgba(17,20,28,0.6); border-radius:8px; border-left:3px solid #889DC4; font-style:italic; color:#B0B8C4; line-height:1.8; }
  .task-block { padding:18px; background:rgba(136,157,196,0.06); border-radius:8px; margin-bottom:14px; }
  .task-title { font-size:14px; font-weight:700; color:#889DC4; margin:0 0 8px; }
  .task-body { font-size:14px; color:#C8CFDE; line-height:1.8; }
  .checklist { list-style:none; padding:0; }
  .checklist li { font-size:13.5px; color:#B0B8C4; padding:4px 0; line-height:1.7; }
  .checklist li::before { content:'□ '; color:#6B7280; }
  .guide-item { font-size:13.5px; color:#B0B8C4; padding:4px 0; line-height:1.7; }
  .footer { padding:24px 32px; background:#0A0D12; font-size:12px; color:#6B7280; text-align:center; line-height:1.8; }
  .footer a { color:#889DC4; }
  .divider { border:none; border-top:1px solid rgba(136,157,196,0.06); margin:0; }
  .section-subtitle { font-size:12px; color:#6B7280; font-style:italic; margin:0 0 16px; }
  .reading-guide { padding:14px 18px; background:rgba(136,157,196,0.05); border-radius:6px; margin-top:16px; font-size:14px; color:#B0B8C4; line-height:1.8; }
  .reading-guide strong { color:#E2E5EC; }
  .sentence-decon { padding:14px 18px; background:rgba(17,20,28,0.6); border-radius:6px; margin-bottom:12px; }
  .code-block { font-family:'SF Mono','Fira Code',monospace; font-size:13px; background:rgba(10,13,18,0.8); padding:12px 16px; border-radius:6px; color:#a5b4cb; line-height:1.6; white-space:pre-wrap; }
  .note-block { font-size:12.5px; color:#88909F; margin-top:10px; line-height:1.7; }
"""


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _markdown_inline_to_html(text: str) -> str:
    """Convert basic markdown inline formatting to HTML."""
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<!\*)\*([^*\n]+?)\*(?!\*)', r'<em>\1</em>', text)
    text = re.sub(r'(?<!_)_([^_\n]+?)_(?!_)', r'<em>\1</em>', text)
    text = re.sub(r'`([^`]+)`', r'<code style="font-family:monospace;background:rgba(0,0,0,0.3);padding:1px 5px;border-radius:3px;font-size:13px;">\1</code>', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" style="color:#889DC4;">\1</a>', text)
    return text


def markdown_to_email_html(md_text: str) -> str:
    """
    Convert ArgueLab v2 briefing markdown to email-safe inline HTML.
    Handles the 6-pane format: sections separated by ## N. headers.
    """
    lines = md_text.strip().split("\n")
    html = []
    i = 0
    current_section = None
    section_lines = []
    in_code_block = False
    code_lines = []

    def flush_section():
        nonlocal section_lines, current_section
        if not section_lines or not current_section:
            section_lines = []
            return
        title = current_section
        html.append(f'<div class="section"><h2 class="section-title">{title}</h2>')

        # Collect rendered paragraphs
        paragraphs = []
        current_para = []
        for s in section_lines:
            if s == "":
                if current_para:
                    paragraphs.append("\n".join(current_para))
                    current_para = []
            else:
                current_para.append(s)
        if current_para:
            paragraphs.append("\n".join(current_para))

        for p in paragraphs:
            p = p.strip()
            if not p:
                continue
            rendered = _markdown_inline_to_html(p)

            # Bold headers in Chinese sections
            if rendered.startswith("<strong>") and "</strong>" in rendered[:100]:
                # Short bold line = sub-header
                content = rendered
                if len(p) < 120:
                    html.append(f'<p class="cn-body"><strong>{p.strip("*")}</strong></p>')
                    continue

            # Italic captions
            if p.startswith("*") and p.endswith("*") and not p.startswith("**"):
                html.append(f'<p class="section-subtitle">{p.strip("*")}</p>')
                continue

            # Blockquote-style lines
            if p.startswith("> "):
                html.append(f'<div class="quote-block">{p[2:]}</div>')
                continue

            # Bullet lists
            if p.startswith("- "):
                items = p.split("\n- ")
                lis = ""
                for item in items:
                    item = item.lstrip("- ").strip()
                    if item:
                        lis += f"<li>{_markdown_inline_to_html(item)}</li>"
                html.append(f'<ul class="checklist">{lis}</ul>')
                continue

            # Code blocks (already processed inline)
            if p.startswith('<div class="code-block"'):
                html.append(p)
                continue

            # Default: paragraph
            if any('\u4e00' <= c <= '\u9fff' for c in p):
                html.append(f'<p class="cn-body">{rendered}</p>')
            else:
                html.append(f'<p class="en-body">{rendered}</p>')

        html.append('</div>')
        section_lines = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Code blocks
        if stripped.startswith("```"):
            if in_code_block:
                code_html = _escape_html("\n".join(code_lines))
                section_lines.append(f'<div class="code-block">{code_html}</div>')
                code_lines = []
                in_code_block = False
            else:
                in_code_block = True
            i += 1
            continue

        if in_code_block:
            code_lines.append(line)
            i += 1
            continue

        # H2: ## N. Section Name
        if re.match(r'^##\s+\d+\.', stripped):
            flush_section()
            current_section = re.sub(r'^##\s+\d+\.\s*', '', stripped)
            section_lines = []
            i += 1
            continue

        # H2 fallback
        if stripped.startswith("## ") and not stripped.startswith("### "):
            flush_section()
            current_section = stripped[3:].strip()
            section_lines = []
            i += 1
            continue

        # H3 sub-headers
        if stripped.startswith("### "):
            sub = _markdown_inline_to_html(stripped[4:].strip())
            section_lines.append(f'<h3 style="font-size:15px;font-weight:700;color:#889DC4;margin:18px 0 8px;">{sub}</h3>')
            i += 1
            continue

        # H4 sub-headers
        if stripped.startswith("#### "):
            sub = _markdown_inline_to_html(stripped[5:].strip())
            section_lines.append(f'<h4 style="font-size:14px;font-weight:600;color:#C8CFDE;margin:14px 0 6px;">{sub}</h4>')
            i += 1
            continue

        # Horizontal rules — skip
        if stripped == "---":
            i += 1
            continue

        # Regular content
        section_lines.append(stripped)
        i += 1

    flush_section()

    return "\n".join(html)


def build_email_html(md_text: str, recipient_name: str = "") -> str:
    """Build full HTML email from ArgueLab v2 briefing markdown."""
    today = datetime.now().strftime("%B %d, %Y")
    weekday = datetime.now().strftime("%A")
    greeting = f"Good morning{', ' + recipient_name if recipient_name else ''}."

    # Extract topic line from H1 metadata
    topic_line = ""
    for line in md_text.split("\n"):
        m = re.match(r'>\s*\*\*今日议题：\*\*\s*(.+)', line.strip())
        if m:
            topic_line = m.group(1)
            break

    body_html = markdown_to_email_html(md_text)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>ArgueLab — {today}</title>
<style>{EMAIL_CSS}</style>
</head>
<body>
<div class="container">
  <div class="header">
    <p class="logo">Argue<span>Lab</span></p>
    <p class="header-date">{today} · {weekday}</p>
    <p class="tagline">Read like a scholar. Argue like a native.</p>
    {f'<p class="topic-line">{topic_line}</p>' if topic_line else ''}
  </div>
  <div class="section">
    <p style="font-size:15px;line-height:1.7;margin:0;color:#E2E5EC;">{greeting}</p>
    <p style="font-size:14px;color:#88909F;line-height:1.6;margin:8px 0 0;">Today's briefing trains your argument skills through a complete learning loop: Background → Passage → Expressions → Sentence Deconstruction → Argument Chain → Output. Focus on the Expression Bank and Output Task sections.</p>
  </div>
  {body_html}
  <div class="footer">
    <p style="margin:0;font-style:italic;">ArgueLab is a product built by learners, for learners. We believe that true language ability is not measured by how much you can read — but by how much you can express.</p>
    <p style="margin:8px 0 0;">You received this because you subscribed. <a href="#">Unsubscribe</a> anytime.</p>
  </div>
</div>
</body>
</html>"""


# ── Email sending ──

def send_briefing_to_all(md_path: str) -> dict:
    """Read briefing markdown and send HTML email to all subscribers."""
    if not SMTP_HOST:
        return {"status": "error", "message": "SMTP not configured. Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS environment variables."}

    subs = load_subscribers()
    if not subs:
        return {"status": "error", "message": "No subscribers found."}

    md_text = Path(md_path).read_text(encoding="utf-8")
    sent, failed = [], []

    for sub in subs:
        try:
            html = build_email_html(md_text, sub.get("name", ""))
            send_email(sub["email"], f"ArgueLab — {datetime.now().strftime('%B %d, %Y')}", html)
            sent.append(sub["email"])
            print(f"  ✓ {sub['email']}")
        except Exception as e:
            failed.append({"email": sub["email"], "error": str(e)})
            print(f"  ✗ {sub['email']}: {e}")

    return {"status": "ok", "sent": len(sent), "failed": failed}


def send_email(to_email: str, subject: str, html_body: str) -> None:
    """Send a single HTML email. Prefers Resend HTTP API (no domain needed);
    falls back to SMTP."""
    if RESEND_API_KEY:
        return _send_via_resend_api(to_email, subject, html_body)
    return _send_via_smtp(to_email, subject, html_body)


def _send_via_resend_api(to_email: str, subject: str, html_body: str) -> None:
    """Send via Resend HTTP API. Works without domain verification —
    uses onboarding@resend.dev as sender (100 emails/day free tier limit)."""
    import urllib.request
    import urllib.error

    data = json.dumps({
        "from": "ArgueLab <onboarding@resend.dev>",
        "to": [to_email],
        "subject": subject,
        "html": html_body
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=data,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "ArgueLab/1.0"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            print(f"  ✓ {to_email} (Resend API — id: {result.get('id', 'N/A')})")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        raise RuntimeError(f"Resend API error {e.code}: {error_body}")
    except Exception as e:
        raise RuntimeError(f"Resend API error: {e}")


def _send_via_smtp(to_email: str, subject: str, html_body: str) -> None:
    """Send a single HTML email via SMTP (legacy fallback)."""
    if not SMTP_HOST:
        raise RuntimeError("Neither RESEND_API_KEY nor SMTP_HOST is configured.")

    msg = MIMEMultipart("alternative")
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg["Subject"] = subject

    # Plain-text fallback
    plain = html_body
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()

    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


# ── HTTP API Server ──

class APIHandler(BaseHTTPRequestHandler):
    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path: str, content_type: str):
        full_path = BASE_DIR / "static" / path
        try:
            content = full_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._send_static("index.html", "text/html; charset=utf-8")
        elif self.path == "/api/subscribers":
            subs = load_subscribers()
            self._send_json({"count": len(subs), "subscribers": [{k: s[k] for k in ("email","name","subscribed_at")} for s in subs]})
        elif self.path == "/api/health":
            self._send_json({"status": "ok", "subscribers": len(load_subscribers()), "smtp_configured": bool(SMTP_HOST)})
        elif self.path == "/api/preview":
            # Show latest briefing preview
            briefing_dir = BASE_DIR.parent / "guardian-agent" / "briefings"
            if briefing_dir.exists():
                files = sorted(briefing_dir.glob("*.md"), reverse=True)
                if files:
                    md = files[0].read_text(encoding="utf-8")
                    html = build_email_html(md, "Preview")
                    self._send_json({"html": html})
                    return
            self._send_json({"error": "No briefing found"}, 404)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b"{}"
        data = json.loads(body)

        if self.path == "/api/subscribe":
            email = data.get("email", "").strip()
            if not email or "@" not in email:
                self._send_json({"status": "error", "message": "Valid email required."}, 400)
                return
            name = data.get("name", "")
            result = add_subscriber(email, name)
            self._send_json(result)
        else:
            self._send_json({"error": "Not found"}, 404)


def run_server():
    server = HTTPServer((SERVER_HOST, SERVER_PORT), APIHandler)
    print(f"Dispatch API server running at http://{SERVER_HOST}:{SERVER_PORT}")
    print(f"Landing page:  http://localhost:{SERVER_PORT}")
    print(f"API health:    http://localhost:{SERVER_PORT}/api/health")
    print(f"SMTP configured: {'Yes' if SMTP_HOST else 'No — set SMTP_HOST to enable email'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


# ── CLI ──

def main():
    parser = argparse.ArgumentParser(description="The Morning Dispatch — backend")
    parser.add_argument("--send", metavar="PATH", help="Send briefing markdown to all subscribers")
    parser.add_argument("--add", metavar="EMAIL", help="Add a new subscriber")
    parser.add_argument("--remove", metavar="EMAIL", help="Remove a subscriber")
    parser.add_argument("--list", action="store_true", help="List all subscribers")
    parser.add_argument("--serve", action="store_true", help="Start the API server")
    parser.add_argument("--preview", metavar="PATH", help="Preview HTML email from markdown file")

    args = parser.parse_args()

    if args.serve:
        run_server()
    elif args.send:
        result = send_briefing_to_all(args.send)
        print(json.dumps(result, indent=2))
    elif args.add:
        print(json.dumps(add_subscriber(args.add), indent=2))
    elif args.remove:
        print(json.dumps(remove_subscriber(args.remove), indent=2))
    elif args.list:
        subs = load_subscribers()
        if not subs:
            print("No subscribers yet.")
        for s in subs:
            status = "✓ verified" if s.get("verified") else "○ pending"
            print(f"  {s['email']}  ({status})  since {s['subscribed_at'][:10]}")
    elif args.preview:
        md = Path(args.preview).read_text(encoding="utf-8")
        html = build_email_html(md, "Reader")
        out_path = Path(args.preview).with_suffix(".html")
        full_html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Email Preview</title>
<style>{EMAIL_CSS}</style>
</head>
<body style="background:#E8E6DD;padding:40px;">
<div style="max-width:600px;margin:0 auto;box-shadow:0 2px 12px rgba(0,0,0,0.08);">{html}</div>
</body>
</html>"""
        out_path.write_text(full_html, encoding="utf-8")
        print(f"Email preview saved → {out_path}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
