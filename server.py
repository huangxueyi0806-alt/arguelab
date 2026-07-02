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
from pathlib import Path

# ── Paths (define early so supabase_client can be loaded by path) ──
BASE_DIR = Path(__file__).parent

# ── Supabase client (primary subscriber store) ──
SUPABASE_IMPORT_ERROR = None
try:
    import importlib.util, sys
    sb_path = BASE_DIR / "supabase_client.py"
    if sb_path.exists():
        spec = importlib.util.spec_from_file_location("supabase_client", sb_path)
        sb_mod = importlib.util.module_from_spec(spec)
        sys.modules["supabase_client"] = sb_mod
        spec.loader.exec_module(sb_mod)
        sb_get_subscribers = sb_mod.get_subscribers
        sb_add_subscriber = sb_mod.add_subscriber
        sb_remove_subscriber = sb_mod.remove_subscriber
        sb_update_subscriber = sb_mod.update_subscriber
        SUPABASE_AVAILABLE = True
    else:
        raise FileNotFoundError(f"supabase_client.py not found at {sb_path}")
except Exception as _e:
    SUPABASE_AVAILABLE = False
    SUPABASE_IMPORT_ERROR = str(_e)
    print(f"[warn] supabase_client not available: {_e} — using local JSON only")

# Auto-load .env file (if python-dotenv is installed)
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

# Feature flags from environment
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "true").lower() in ("true", "1", "yes")

DATA_DIR = BASE_DIR / "data"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
TEMPLATES_DIR = BASE_DIR / "templates"

DATA_DIR.mkdir(exist_ok=True)

# ── Issues storage ──
ISSUES_FILE = DATA_DIR / "issues.json"

def load_issues() -> list[dict]:
    if not ISSUES_FILE.exists():
        return []
    return json.loads(ISSUES_FILE.read_text(encoding="utf-8"))

def save_issues(issues: list[dict]) -> None:
    ISSUES_FILE.write_text(json.dumps(issues, indent=2, ensure_ascii=False), encoding="utf-8")

def upsert_issue(slug: str, data: dict) -> dict:
    """Create or update an issue by slug."""
    issues = load_issues()
    existing = next((i for i in issues if i.get("slug") == slug), None)
    now = datetime.now().isoformat()
    if existing:
        existing.update(data)
        existing["updated_at"] = now
    else:
        issue = {"slug": slug, "created_at": now, "updated_at": now, **data}
        issues.append(issue)
    save_issues(issues)
    return next(i for i in load_issues() if i.get("slug") == slug)


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
    """Load subscribers from Supabase (primary) with local JSON fallback."""
    if SUPABASE_AVAILABLE:
        try:
            return sb_get_subscribers()
        except Exception as e:
            print(f"[supabase] load failed: {e} — falling back to local JSON")
    if not SUBSCRIBERS_FILE.exists():
        return []
    return json.loads(SUBSCRIBERS_FILE.read_text())


def save_subscribers(subs: list[dict]) -> None:
    """Persist subscribers. With Supabase, we sync FROM Supabase TO local JSON.
    Call this after any Supabase write to keep the local cache fresh."""
    if SUPABASE_AVAILABLE:
        try:
            from supabase_client import _sync_to_local_json
            _sync_to_local_json()
            return
        except Exception:
            pass
    SUBSCRIBERS_FILE.write_text(json.dumps(subs, indent=2, ensure_ascii=False))


def add_subscriber(email: str, name: str = "", **kwargs) -> dict:
    """Add subscriber via Supabase (primary), with local JSON fallback."""
    email = email.strip().lower()
    if SUPABASE_AVAILABLE:
        try:
            result = sb_add_subscriber(email, name, **kwargs)
            if result.get("status") in ("exists", "ok"):
                save_subscribers(load_subscribers())  # refresh local cache
                return result
        except Exception as e:
            print(f"[supabase] add failed: {e} — falling back to local JSON")
    # Fallback: local JSON only
    subs = load_subscribers()
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
    """Remove subscriber via Supabase (primary), with local JSON fallback."""
    email = email.strip().lower()
    if SUPABASE_AVAILABLE:
        try:
            result = sb_remove_subscriber(email)
            if result.get("status") in ("ok", "not_found"):
                save_subscribers(load_subscribers())  # refresh local cache
                return result
        except Exception as e:
            print(f"[supabase] remove failed: {e} — falling back to local JSON")
    # Fallback: local JSON only
    subs = load_subscribers()
    new = [s for s in subs if s["email"] != email]
    if len(new) == len(subs):
        return {"status": "not_found", "email": email}
    save_subscribers(new)
    return {"status": "ok", "email": email}


# ── Remote subscriber sync ──
# Before sending emails, pull subscribers from Railway and merge.
# This ensures signups from the landing page aren't lost.

def fetch_remote_subscribers() -> list[dict]:
    """Fetch subscriber list from the Railway deployment.
    Returns empty list if unreachable (Railway may be down, deploying, etc.)."""
    import urllib.request
    import urllib.error
    base = os.environ.get("BASE_URL", f"http://localhost:{SERVER_PORT}")
    url = f"{base}/api/subscribers"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("subscribers", [])
    except Exception:
        # Railway unreachable — that's fine, fall back to local
        return []


def merge_subscribers(local: list[dict], remote: list[dict]) -> list[dict]:
    """Merge remote subscribers into local, keeping whichever has more info.
    Returns merged list (not saved to disk)."""
    merged = {s["email"].strip().lower(): dict(s) for s in local}
    for s in remote:
        email = s.get("email", "").strip().lower()
        if not email:
            continue
        if email in merged:
            # Keep the entry with more fields or newer timestamp
            existing = merged[email]
            remote_ts = s.get("subscribed_at", "")
            local_ts = existing.get("subscribed_at", "")
            if remote_ts > local_ts:
                merged[email].update(s)
        else:
            merged[email] = dict(s)
    return list(merged.values())


def push_subscribers_to_remote(subs: list[dict]) -> bool:
    """Push the merged subscriber list back to Railway so it persists."""
    import urllib.request
    import urllib.error
    base = os.environ.get("BASE_URL", f"http://localhost:{SERVER_PORT}")
    url = f"{base}/api/subscribers/sync"
    data = json.dumps({"subscribers": subs}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="PUT",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True
    except Exception:
        return False


# ── Email template builder ──

EMAIL_CSS = """
  /* ── Reset & Base ── */
  body { margin:0; padding:0; background:#F3F5F8; -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }
  .ReadMsgBody { width:100%; }
  .ExternalClass { width:100%; }

  /* ── Card ── */
  .email-card {
    background: radial-gradient(ellipse 100% 80px at 50% 0%, rgba(96,165,250,0.10), transparent 35%), #0B0F14;
    border-radius:18px;
    border:1px solid rgba(148,163,184,0.16);
    overflow:hidden;
  }

  /* ── Typography ── */
  .logo {
    font-family:Georgia,'Times New Roman',serif;
    font-size:32px;
    font-weight:700;
    color:#E2E5EC;
    letter-spacing:-0.02em;
  }
  .logo .lab { color:#889DC4; }
  .tagline {
    font-family:Georgia,'Times New Roman',serif;
    font-size:13px;
    color:#7C8798;
    font-style:italic;
  }

  .greeting {
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Noto Sans SC',sans-serif;
    font-size:15px;
    color:#C8CFDE;
    line-height:1.7;
  }

  .label {
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Noto Sans SC',sans-serif;
    font-size:11px;
    color:#6B7280;
    letter-spacing:1.5px;
    font-weight:600;
    margin:0 0 10px;
  }

  .issue-value, .focus-value {
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Noto Sans SC',sans-serif;
    font-size:16px;
    color:#E2E5EC;
    line-height:1.7;
    margin:0;
  }

  .issue-accent {
    border-left:2px solid rgba(136,157,196,0.25);
    padding-left:14px;
  }

  /* ── Practice mini-rows ── */
  .practice-row td {
    padding:10px 0;
    border-bottom:1px solid rgba(148,163,184,0.06);
  }
  .practice-row:last-child td { border-bottom:none; }
  .practice-dot {
    display:inline-block;
    width:5px;
    height:5px;
    border-radius:50%;
    background:rgba(148,163,184,0.45);
    margin-right:12px;
    vertical-align:middle;
  }
  .practice-text {
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Noto Sans SC',sans-serif;
    font-size:14px;
    color:#B0B8C4;
    line-height:1.7;
  }

  /* ── Buttons ── */
  .btn {
    display:inline-block;
    height:46px;
    line-height:46px;
    padding:0 26px;
    border-radius:10px;
    font-size:14.5px;
    font-weight:700;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Noto Sans SC',sans-serif;
    letter-spacing:0.01em;
    text-decoration:none;
    margin:0 6px;
    transition:transform 0.15s ease, box-shadow 0.15s ease;
    -webkit-transition:transform 0.15s ease, box-shadow 0.15s ease;
  }
  .btn-primary {
    background: linear-gradient(180deg, #E7EEF8 0%, #B8C7DD 100%) !important;
    color:#0B0F14 !important;
    border:1px solid rgba(255,255,255,0.45);
    box-shadow: 0 8px 20px rgba(15,23,42,0.35), inset 0 1px 0 rgba(255,255,255,0.75);
  }
  .btn-secondary {
    background:rgba(15,23,42,0.45) !important;
    color:#B8C7DD !important;
    border:1px solid rgba(148,163,184,0.34);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
  }
  .btn:hover {
    transform:translateY(-1px);
    -webkit-transform:translateY(-1px);
  }
  .btn-primary:hover {
    box-shadow: 0 10px 24px rgba(15,23,42,0.45), inset 0 1px 0 rgba(255,255,255,0.85);
  }
  .btn-secondary:hover {
    border-color:rgba(184,199,221,0.5);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.08);
  }
  .btn:active {
    transform:translateY(0);
    -webkit-transform:translateY(0);
  }

  /* ── Subtext ── */
  .subtext {
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Noto Sans SC',sans-serif;
    font-size:12px;
    color:#7C8798;
    margin:0;
    text-align:center;
  }

  /* ── Divider ── */
  .divider { border:none; border-top:1px solid rgba(148,163,184,0.12); }

  /* ── Footer ── */
  .footer-text {
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Noto Sans SC',sans-serif;
    font-size:12px;
    color:#7C8798;
    line-height:1.8;
    margin:0;
    text-align:center;
  }
  .footer-text a { color:#7C8798; text-decoration:underline; }

  /* ── Mobile ── */
  @media only screen and (max-width:640px) {
    .email-card { border-radius:0 !important; }
    .logo { font-size:28px !important; }
    .btn-row td { display:block !important; width:100% !important; text-align:center !important; padding:6px 0 !important; }
    .btn { display:block !important; width:auto !important; margin:4px auto !important; }
  }
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


def build_email_html(md_text: str, issue_number: int = 1, read_url: str = "", pdf_url: str = "", recipient_name: str = "", unsubscribe_url: str = "") -> tuple:
    """Build ArgueLab training-card notification email from briefing markdown.

    Parses optional YAML frontmatter (read_url / pdf_url / issue_number), extracts
    metadata (topic, training focus, practice items), and produces a table-based
    editorial-dark HTML email with polished CTA buttons plus a plain-text fallback.

    Returns (subject: str, html: str, text: str).
    """

    # ── YAML frontmatter ──
    fm_read_url = ""
    fm_pdf_url = ""
    fm_issue_number = None
    if md_text.startswith("---"):
        end_fm = md_text.index("---", 3)
        fm_text = md_text[3:end_fm]
        for line in fm_text.splitlines():
            if line.startswith("read_url:"):
                fm_read_url = line[len("read_url:"):].strip()
            elif line.startswith("pdf_url:"):
                fm_pdf_url = line[len("pdf_url:"):].strip()
            elif line.startswith("issue_number:"):
                try:
                    fm_issue_number = int(line[len("issue_number:"):].strip())
                except ValueError:
                    pass
        md_text = md_text[end_fm + 3:].strip()

    if not read_url:
        read_url = fm_read_url
    if not pdf_url:
        pdf_url = fm_pdf_url
    if issue_number == 1 and fm_issue_number is not None:
        issue_number = fm_issue_number

    # ── Metadata extraction ──
    topic_line = ""
    training_focus = ""
    practice_items = []
    date_str = ""
    briefing_date = ""

    lines = md_text.split("\n")
    in_expressions = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # ── Date from title: `# ArgueLab Training Briefing — YYYY-MM-DD` ──
        m = re.match(r'^#\s+ArgueLab.*[—–·-]\s*(\d{4}-\d{2}-\d{2})', stripped)
        if m:
            briefing_date = m.group(1)
            # Format as "June 28, 2026"
            try:
                dt = datetime.strptime(briefing_date, "%Y-%m-%d")
                date_str = dt.strftime("%B %d, %Y")
            except ValueError:
                date_str = briefing_date
            # Also set issue_number from date
            if issue_number == 1:
                issue_number = int(briefing_date.replace("-", ""))
            continue

        # ── Topic: `**议题：** ...` or `**今日议题：** ...` (with optional `>` prefix) ──
        m = re.match(r'>?\s*\*\*(?:今日)?议题：?\*\*\s*(.+)', stripped)
        if m:
            topic_line = m.group(1).strip()
            continue

        # ── Training focus: from `**为什么选这个议题：** ...训练的是...` ──
        m = re.match(r'>?\s*\*\*为什么选这个议题：?\*\*\s*(.+)', stripped)
        if m:
            why_text = m.group(1).strip()
            # Extract the part after "训练的是"
            fm = re.search(r'训练的是(.+?)[。.]?\s*$', why_text)
            if fm:
                training_focus = fm.group(1).strip()
            else:
                # Fallback: use first 60 chars
                training_focus = why_text[:80]
            continue

        # ── Training focus fallback: `**Framing 提示：**` ──
        if not training_focus:
            m = re.match(r'>?\s*\*\*训练重点：?\*\*\s*(.+)', stripped)
            if m:
                training_focus = m.group(1).strip()
                continue

        # ── Practice items: from expression cards ──
        if re.match(r'^##\s*(?:\d+\.?\s*)?(?:5个|5\s*个)?可迁移表达', stripped) or re.match(r'^##\s*(?:\d+\.?\s*)?5\s*[个項]?\s*Express', stripped, re.IGNORECASE):
            in_expressions = True
            continue
        if in_expressions and re.match(r'^##\s', stripped):
            in_expressions = False
            continue
        if in_expressions and len(practice_items) < 5:
            # New format: `### N. phrase`
            m = re.match(r'^###\s*\d+\.\s*(.+)', stripped)
            if m:
                phrase = m.group(1).strip()
                cn_label = ""
                for j in range(i+1, min(i+8, len(lines))):
                    next_line = lines[j].strip()
                    cm = re.match(r'\*\*(?:功能|语域|语义|标签|适用)[^*]*\*\*\s*[：:]\s*(.+?)(?:\s*\|\s*.+)?$', next_line)
                    if cm:
                        cn_label = cm.group(1).strip()
                        break
                    # Old format: `**英文表达：**` ... `**功能标签：**` ...
                    cm2 = re.match(r'\*\*(?:功能标签|中文释义)[：:]\*\*\s*(.+)', next_line)
                    if cm2:
                        cn_label = cm2.group(1).strip()
                        break
                if cn_label:
                    practice_items.append(f"{phrase} — {cn_label}")
                else:
                    practice_items.append(phrase)
                continue
            # Old format: `### 表达 N` → phrase is in next `**英文表达：**`
            m_old = re.match(r'^###\s*表达\s*\d+', stripped)
            if m_old:
                phrase = ""
                cn_label = ""
                for j in range(i+1, min(i+15, len(lines))):
                    next_line = lines[j].strip()
                    if next_line.startswith('---') or next_line.startswith('## ') or re.match(r'^###\s*表达\s*\d+', next_line):
                        break
                    pm = re.match(r'\*\*英文表达：?\*\*\s*`?(.+?)`?\s*$', next_line)
                    if pm:
                        phrase = pm.group(1).strip().strip('`')
                    cm = re.match(r'\*\*(?:功能标签|中文释义)[：:]\*\*\s*(.+)', next_line)
                    if cm and not cn_label:
                        cn_label = cm.group(1).strip()
                if phrase:
                    if cn_label:
                        practice_items.append(f"{phrase} — {cn_label}")
                    else:
                        practice_items.append(phrase)
                continue

    # ── Fallback: practice items from Framing or expression list ──
    if not practice_items:
        for line in lines:
            stripped = line.strip()
            # Match expression card headers
            m = re.match(r'^###\s*\d+\.\s*(.+)', stripped)
            if m and len(practice_items) < 5:
                practice_items.append(m.group(1).strip())
                continue

    if not date_str:
        date_str = datetime.now().strftime("%B %d, %Y")

    # Auto-generate read_url and pdf_url from briefing date if not provided
    if (not read_url or read_url == "#") and briefing_date:
        base = os.environ.get("BASE_URL", f"http://localhost:{SERVER_PORT}")
        read_url = f"{base}/issues/{briefing_date}"
        pdf_url = f"{base}/issues/{briefing_date}/download"

    # ── Subject & greeting ──
    issue_title_short = topic_line if topic_line else f"Issue #{issue_number:03d}"
    subject = f"ArgueLab #{issue_number:03d} | {issue_title_short}"
    greeting = "今天的训练卡已经生成。" if not recipient_name else f"{recipient_name}，今天的训练卡已经生成。"

    # ── Plain-text fallback ──
    text = subject + "\n" + "—" * len(subject) + "\n\n"
    text += greeting + "\n\n"
    text += "TODAY'S ISSUE\n" + issue_title_short + "\n\n"
    if training_focus:
        text += "TRAINING FOCUS\n" + training_focus + "\n\n"
    if practice_items:
        text += "WHAT YOU WILL PRACTICE\n"
        for item in practice_items[:3]:
            text += "  \u2022  " + item + "\n"
        text += "\n"
    text += "Read Online: " + (read_url or "#") + "\n"
    if pdf_url and pdf_url != "#":
        text += "Download PDF: " + pdf_url + "\n"
    text += "\n"
    text += "Core material is available online"
    if pdf_url and pdf_url != "#":
        text += " and as a downloadable PDF"
    text += ".\n\n"
    text += "—\n"
    text += "No spam. You can unsubscribe anytime: " + (unsubscribe_url or "#") + "\n"
    text += "You received this because you joined ArgueLab Beta.\n"

    # ── HTML: practice mini-rows ──
    practice_rows_html = ""
    items_to_show = practice_items[:3] if practice_items else ["外刊表达拆解与迁移训练"]
    for i, item in enumerate(items_to_show):
        border = "border-bottom:1px solid rgba(148,163,184,0.06);" if i < len(items_to_show) - 1 else ""
        practice_rows_html += (
            '<tr class="practice-row">'
            '<td style="padding:10px 0;' + border + '">'
            '<span class="practice-dot" style="display:inline-block;width:5px;height:5px;border-radius:50%;'
            'background:rgba(148,163,184,0.45);margin-right:12px;vertical-align:middle;"></span>'
            '<span class="practice-text" style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
            '\'PingFang SC\',\'Noto Sans SC\',sans-serif;font-size:14px;color:#B0B8C4;line-height:1.7;">'
            + _escape_html(item) + '</span>'
            '</td>'
            '</tr>'
        )

    # ── HTML: buttons ──
    read_link = read_url or "#"
    has_pdf = bool(pdf_url and pdf_url != "#")
    unsub_link = unsubscribe_url or "#"

    read_btn = (
        '<a href="' + read_link + '" class="btn btn-primary"'
        ' style="display:inline-block;height:46px;line-height:46px;padding:0 26px;border-radius:10px;'
        'font-size:14.5px;font-weight:700;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
        '\'PingFang SC\',\'Noto Sans SC\',sans-serif;letter-spacing:0.01em;text-decoration:none;'
        'background:#D4DCE8;color:#0B0F14;border:1px solid rgba(255,255,255,0.45);'
        'box-shadow:0 8px 20px rgba(15,23,42,0.35),inset 0 1px 0 rgba(255,255,255,0.75);'
        '">Read Online</a>'
    )

    pdf_btn_td = ""
    pdf_subtext = ""
    if has_pdf:
        pdf_btn_td = (
            '<td style="padding:0 6px;">'
            '<a href="' + pdf_url + '" class="btn btn-secondary"'
            ' style="display:inline-block;height:46px;line-height:46px;padding:0 26px;border-radius:10px;'
            'font-size:14.5px;font-weight:700;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
            '\'PingFang SC\',\'Noto Sans SC\',sans-serif;letter-spacing:0.01em;text-decoration:none;'
            'background:rgba(15,23,42,0.45);color:#B8C7DD;border:1px solid rgba(148,163,184,0.34);'
            'box-shadow:inset 0 1px 0 rgba(255,255,255,0.04);'
            '">Download PDF</a>'
            '</td>'
        )
        pdf_subtext = " and as a downloadable PDF"

    # ── HTML: assemble table-based template ──
    html = (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">\n'
        '<title>{title}</title>\n'
        '<style>{css}</style>\n'
        '</head>\n'
        '<body style="margin:0;padding:0;background:#F3F5F8;">\n'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">\n'
        '<tr>\n'
        '<td align="center" style="padding:40px 16px;">\n'

        # ── Card (bgcolor on <td> not <table> — Gmail strips table bgcolor) ──
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="620"'
        ' class="email-card"'
        ' style="max-width:620px;border-radius:18px;border:1px solid rgba(148,163,184,0.16);overflow:hidden;">\n'
        '<tr>\n'
        '<td bgcolor="#0B0F14" style="background-color:#0B0F14;border-radius:18px;padding:0;">\n'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">\n'

        # ── Header: Logo + Tagline ──
        '<tr>\n'
        '<td align="center" style="padding:44px 40px 8px;">\n'
        '<span class="logo" style="font-family:Georgia,\'Times New Roman\',serif;font-size:32px;font-weight:700;'
        'color:#E2E5EC;letter-spacing:-0.02em;">Argue<span class="lab" style="color:#889DC4;">Lab</span></span><br>\n'
        '<span class="tagline" style="font-family:Georgia,\'Times New Roman\',serif;font-size:13px;'
        'color:#7C8798;font-style:italic;display:inline-block;margin-top:8px;">'
        'Read like a scholar. Argue like a native.</span>\n'
        '</td>\n'
        '</tr>\n'

        # ── Header divider ──
        '<tr>\n'
        '<td style="padding:0 40px 28px;">\n'
        '<hr class="divider" style="border:none;border-top:1px solid rgba(148,163,184,0.14);margin:0;">\n'
        '</td>\n'
        '</tr>\n'

        # ── Greeting ──
        '<tr>\n'
        '<td style="padding:0 40px 28px;">\n'
        '<p class="greeting" style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',\'PingFang SC\','
        '\'Noto Sans SC\',sans-serif;font-size:15px;color:#C8CFDE;line-height:1.7;margin:0;">{greeting}</p>\n'
        '</td>\n'
        '</tr>\n'

        # ── TODAY'S ISSUE ──
        '<tr>\n'
        '<td style="padding:0 40px 28px;">\n'
        '<p class="label" style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',\'PingFang SC\','
        '\'Noto Sans SC\',sans-serif;font-size:11px;color:#6B7280;letter-spacing:1.5px;font-weight:600;'
        'margin:0 0 10px;">TODAY\'S ISSUE</p>\n'
        '<div class="issue-accent" style="border-left:2px solid rgba(136,157,196,0.25);padding-left:14px;">\n'
        '<p class="issue-value" style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',\'PingFang SC\','
        '\'Noto Sans SC\',sans-serif;font-size:16px;color:#E2E5EC;line-height:1.7;margin:0;">{issue_title}</p>\n'
        '</div>\n'
        '</td>\n'
        '</tr>\n'

        # ── TRAINING FOCUS ──
        '<tr>\n'
        '<td style="padding:0 40px 28px;">\n'
        '<p class="label" style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',\'PingFang SC\','
        '\'Noto Sans SC\',sans-serif;font-size:11px;color:#6B7280;letter-spacing:1.5px;font-weight:600;'
        'margin:0 0 10px;">TRAINING FOCUS</p>\n'
        '<p class="focus-value" style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',\'PingFang SC\','
        '\'Noto Sans SC\',sans-serif;font-size:16px;color:#E2E5EC;line-height:1.7;margin:0;">{training_focus}</p>\n'
        '</td>\n'
        '</tr>\n'

        # ── WHAT YOU WILL PRACTICE ──
        '<tr>\n'
        '<td style="padding:0 40px 28px;">\n'
        '<p class="label" style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',\'PingFang SC\','
        '\'Noto Sans SC\',sans-serif;font-size:11px;color:#6B7280;letter-spacing:1.5px;font-weight:600;'
        'margin:0 0 10px;">WHAT YOU WILL PRACTICE</p>\n'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">\n'
        '{practice_rows}\n'
        '</table>\n'
        '</td>\n'
        '</tr>\n'

        # ── Buttons ──
        '<tr>\n'
        '<td align="center" style="padding:0 40px 32px;">\n'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" class="btn-row"'
        ' style="margin:0 auto;">\n'
        '<tr>\n'
        '<td style="padding:0 6px;">{read_btn}</td>\n'
        '{pdf_btn_td}\n'
        '</tr>\n'
        '</table>\n'
        '</td>\n'
        '</tr>\n'

        # ── Subtext ──
        '<tr>\n'
        '<td align="center" style="padding:0 40px 32px;">\n'
        '<p class="subtext" style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',\'PingFang SC\','
        '\'Noto Sans SC\',sans-serif;font-size:12px;color:#7C8798;margin:0;">'
        'Core material is available online{pdf_subtext}.</p>\n'
        '</td>\n'
        '</tr>\n'

        # ── Footer divider ──
        '<tr>\n'
        '<td style="padding:0 40px 20px;">\n'
        '<hr class="divider" style="border:none;border-top:1px solid rgba(148,163,184,0.12);margin:0;">\n'
        '</td>\n'
        '</tr>\n'

        # ── Footer ──
        '<tr>\n'
        '<td align="center" style="padding:0 40px 36px;">\n'
        '<p class="footer-text" style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',\'PingFang SC\','
        '\'Noto Sans SC\',sans-serif;font-size:12px;color:#7C8798;line-height:1.8;margin:0;">'
        'No spam. You can <a href="{unsub_link}" style="color:#7C8798;text-decoration:underline;">unsubscribe</a> anytime.</p>\n'
        '<p class="footer-text" style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',\'PingFang SC\','
        '\'Noto Sans SC\',sans-serif;font-size:12px;color:#7C8798;line-height:1.8;margin:8px 0 0;">'
        'You received this because you joined ArgueLab Beta.</p>\n'
        '</td>\n'
        '</tr>\n'

        '</table>\n'  # end inner card content table
        '</td>\n'     # end wrapper td with bgcolor
        '</tr>\n'
        '</table>\n'  # end card table
        '</td>\n'
        '</tr>\n'
        '</table>\n'  # end centering table
        '</body>\n'
        '</html>'
    ).format(
        title=_escape_html(subject),
        css=EMAIL_CSS,
        greeting=_escape_html(greeting),
        issue_title=_escape_html(issue_title_short),
        training_focus=_escape_html(training_focus) if training_focus else "—",
        practice_rows=practice_rows_html,
        read_btn=read_btn,
        pdf_btn_td=pdf_btn_td,
        pdf_subtext=pdf_subtext,
        unsub_link=unsub_link,
    )

    return subject, html, text


def markdown_to_email_html(md_text: str) -> str:
    """Legacy: kept for --preview compatibility. Now unused by build_email_html."""
    return build_email_html(md_text, issue_number=0)[1]


# ── Email sending ──

def _extract_date_from_path(md_path: str) -> str:
    """Extract YYYY-MM-DD date from briefing filename like '2026-06-16-briefing.md'."""
    import re as _re
    name = Path(md_path).stem
    m = _re.match(r'^(\d{4}-\d{2}-\d{2})', name)
    return m.group(1) if m else ""


def _get_briefing_dir() -> Path:
    """Return briefing directory, with fallback for Railway deployment.
    
    Local dev: BASE_DIR.parent / "guardian-agent" / "briefings"
    Railway:   BASE_DIR / "briefings" (briefings are copied into the repo)
    """
    primary = BASE_DIR.parent / "guardian-agent" / "briefings"
    if primary.exists():
        return primary
    fallback = BASE_DIR / "briefings"
    if fallback.exists():
        return fallback
    return primary  # return primary anyway; caller handles non-existence

def send_briefing_to_all(md_path: str, issue_number: int = 1, read_url: str = "", pdf_url: str = "") -> dict:
    """Read briefing markdown and send concise notification email to all subscribers.
    
    If read_url/pdf_url are not provided, they are auto-generated as:
      http://localhost:8080/issues/YYYY-MM-DD
      http://localhost:8080/issues/YYYY-MM-DD/download
    """
    if not SMTP_HOST:
        return {"status": "error", "message": "SMTP not configured. Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS environment variables."}

    # ── Sync: pull Railway subscribers, merge with local ──
    local_subs = load_subscribers()
    remote_subs = fetch_remote_subscribers()
    subs = merge_subscribers(local_subs, remote_subs)
    if remote_subs:
        print(f"  (synced {len(remote_subs)} remote subscribers, merged total: {len(subs)})")
    if not subs:
        return {"status": "error", "message": "No subscribers found."}

    md_text = Path(md_path).read_text(encoding="utf-8")

    # Auto-generate URLs from briefing date if not provided
    issue_date = _extract_date_from_path(md_path)
    if not read_url and issue_date:
        base = os.environ.get("BASE_URL", f"http://localhost:{SERVER_PORT}")
        read_url = f"{base}/issues/{issue_date}"
        pdf_url = f"{base}/issues/{issue_date}/download"

    # Build base URL once for unsubscribe links
    base = os.environ.get("BASE_URL", f"http://localhost:{SERVER_PORT}")

    sent, failed = [], []

    for sub in subs:
        try:
            unsub = f"{base}/unsubscribe?token={sub.get('token', '')}"
            subject, html, text = build_email_html(
                md_text,
                issue_number=issue_number,
                read_url=read_url,
                pdf_url=pdf_url,
                recipient_name=sub.get("name", ""),
                unsubscribe_url=unsub,
            )
            send_email(sub["email"], subject, html)
            sent.append(sub["email"])
            print(f"  ✓ {sub['email']}")
        except Exception as e:
            failed.append({"email": sub["email"], "error": str(e)})
            print(f"  ✗ {sub['email']}: {e}")

    # ── Save merged subscribers locally and push to Railway ──
    if len(subs) > len(local_subs):
        save_subscribers(subs)
        print(f"  (saved {len(subs)} subscribers locally)")
        if remote_subs or len(subs) > len(local_subs):
            ok = push_subscribers_to_remote(subs)
            if ok:
                print(f"  (pushed to Railway)")
            else:
                print(f"  (⚠ Railway unreachable, sync on next send)")

    return {"status": "ok", "sent": len(sent), "failed": failed}


def _customize_email_html(html: str, recipient_name: str = "", unsubscribe_url: str = "") -> str:
    """Customize email HTML per-recipient: inject name + unique unsubscribe link.

    Replaces ``{greeting}`` and ``{unsub_link}`` placeholders if present.
    Falls back to regex replacement of common greeting patterns and the
    unsubscribe anchor tag.
    """
    # ── Greeting ──
    if "{greeting}" in html:
        if recipient_name:
            greeting = f"{recipient_name}，今天的训练卡已经生成。"
        else:
            greeting = "今天的训练卡已经生成。"
        html = html.replace("{greeting}", greeting)
    else:
        # Best-effort: replace the default greeting text
        if recipient_name:
            html = re.sub(
                r'(<p class="greeting"[^>]*>)[^<]+(，今天的训练卡已经生成。)</p>',
                lambda m: m.group(1) + recipient_name + "，今天的训练卡已经生成。" + "</p>",
                html,
                count=1
            )

    # ── Unsubscribe link ──
    if "{unsub_link}" in html:
        html = html.replace("{unsub_link}", unsubscribe_url or "#")
    elif unsubscribe_url:
        # Replace the first unsubscribe anchor's href
        html = re.sub(
            r'href="[^"]*unsubscribe[^"]*"',
            f'href="{unsubscribe_url}"',
            html,
            count=1
        )

    return html


def send_email_from_html(html_path: str) -> dict:
    """Send email to all subscribers, re-generating per-recipient from briefing markdown.

    The HTML file path is used to derive the corresponding briefing markdown
    (same directory, ``YYYY-MM-DD-email.html`` → ``YYYY-MM-DD-briefing.md``).
    For each subscriber, :func:`build_email_html` is called with their name
    and a unique unsubscribe link, so every recipient gets a personalized email.

    If the briefing markdown cannot be found, falls back to sending the
    HTML file as-is (with a warning printed).
    """
    if not SMTP_HOST:
        return {"status": "error", "message": "SMTP not configured. Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS environment variables."}

    html_path = Path(html_path)
    if not html_path.exists():
        return {"status": "error", "message": f"File not found: {html_path}"}

    # ── Derive briefing markdown path ──
    briefing_path = None
    m = re.match(r'^(\d{4}-\d{2}-\d{2})-email\.html$', html_path.name)
    date_str = m.group(1) if m else ""
    if date_str:
        for candidate_dir in [
            html_path.parent,
            html_path.parent.parent / "guardian-agent" / "briefings",
            html_path.parent / "briefings",
        ]:
            candidate = candidate_dir / f"{date_str}-briefing.md"
            if candidate.exists():
                briefing_path = candidate
                break

    # ── Extract subject from HTML file (for fallback) ──
    html_template = html_path.read_text(encoding="utf-8")
    subject = "ArgueLab Daily Briefing"
    title_match = re.search(r'<title>(.+?)</title>', html_template, re.IGNORECASE)
    if title_match:
        subject = title_match.group(1).strip()

    base = os.environ.get("BASE_URL", f"http://localhost:{SERVER_PORT}")

    # Sync subscribers
    local_subs = load_subscribers()
    remote_subs = fetch_remote_subscribers()
    subs = merge_subscribers(local_subs, remote_subs)
    if remote_subs:
        print(f"  (synced {len(remote_subs)} remote subscribers, merged total: {len(subs)})")
    if not subs:
        return {"status": "error", "message": "No subscribers found."}

    sent, failed = [], []
    if briefing_path:
        print(f"  (re-generating per-recipient from {briefing_path.name})")
        md_text = briefing_path.read_text(encoding="utf-8")
        read_url  = f"{base}/issues/{date_str}"
        pdf_url   = f"{base}/issues/{date_str}/download"
        for sub in subs:
            try:
                unsub = f"{base}/unsubscribe?token={sub.get('token', '')}"
                _, html_body, _ = build_email_html(
                    md_text,
                    issue_number=int(date_str.replace("-", "")),
                    read_url=read_url,
                    pdf_url=pdf_url,
                    recipient_name=sub.get("name", ""),
                    unsubscribe_url=unsub,
                )
                send_email(sub["email"], subject, html_body)
                sent.append(sub["email"])
                print(f"  ✓ {sub['email']}")
            except Exception as e:
                failed.append({"email": sub["email"], "error": str(e)})
                print(f"  ✗ {sub['email']}: {e}")
    else:
        print(f"  ⚠ Briefing markdown not found for {html_path.name}; sending as-is")
        for sub in subs:
            try:
                send_email(sub["email"], subject, html_template)
                sent.append(sub["email"])
                print(f"  ✓ {sub['email']}")
            except Exception as e:
                failed.append({"email": sub["email"], "error": str(e)})
                print(f"  ✗ {sub['email']}: {e}")

    # Save merged subscribers
    if len(subs) > len(local_subs):
        save_subscribers(subs)
        print(f"  (saved {len(subs)} subscribers locally)")
        ok = push_subscribers_to_remote(subs)
        if ok:
            print(f"  (pushed to Railway)")
        else:
            print(f"  (⚠ Railway unreachable)")

    return {"status": "ok", "sent": len(sent), "failed": failed}



def build_and_save_email(md_path: str, output_path: str = "",
                          read_url: str = "", pdf_url: str = "",
                          issue_number: int = 1) -> dict:
    """Generate email HTML from a briefing markdown and save to file.
    
    Returns dict with: output_path, subject, preview_text (first 200 chars of body text).
    The HTML file is a complete document with <title> for subject extraction.
    """
    md_text = Path(md_path).read_text(encoding="utf-8")

    if not output_path:
        output_path = str(Path(md_path).with_suffix(".email.html"))

    subject, html, text = build_email_html(
        md_text,
        issue_number=issue_number,
        read_url=read_url or "#",
        pdf_url=pdf_url or "#",
        recipient_name="Reader",
        unsubscribe_url="#",
    )

    full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{_escape_html(subject)}</title>
<style>{EMAIL_CSS}</style>
</head>
<body style="background:#E8E6DD;margin:0;padding:0;">
{html}
</body>
</html>"""

    Path(output_path).write_text(full_html, encoding="utf-8")

    return {
        "output_path": output_path,
        "subject": subject,
        "preview_text": text[:200].replace("\n", " ") + "..."
    }


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


# ── Issue Page Renderer ──

ISSUE_PAGE_CSS = r"""
  /* ═══════════════════════════════════════════════
     ArgueLab — Dual-Theme Design System
     Default: Light Reading Mode (paper-like)
     Toggle:  Dark Mode (editorial night)
     ═══════════════════════════════════════════════ */

  /* ── Light Theme (default) ── */
  :root {
    /* Surfaces — paper reading mode */
    --bg: #F7F4ED;
    --surface: #FFFCF7;
    --card-bg: #FFFFFF;
    --card-elevated: #FBF8F1;
    /* Ink — warm, readable */
    --ink: #1F2933;
    --ink-dim: #5E6673;
    --ink-muted: #8A8F98;
    /* Functional Module Colors — richer for light bg */
    --color-context: #3B6EA8;
    --color-context-soft: rgba(59,110,168,0.07);
    --color-context-border: rgba(59,110,168,0.16);
    --color-passage: #3D6A9E;
    --color-passage-soft: rgba(61,106,158,0.07);
    --color-passage-border: rgba(61,106,158,0.16);
    --color-expression: #A67C2E;
    --color-expression-soft: rgba(166,124,46,0.07);
    --color-expression-border: rgba(166,124,46,0.18);
    --color-sentence: #9E5670;
    --color-sentence-soft: rgba(158,86,112,0.07);
    --color-sentence-border: rgba(158,86,112,0.18);
    --color-chain: #3A7D6A;
    --color-chain-soft: rgba(58,125,106,0.07);
    --color-chain-border: rgba(58,125,106,0.18);
    --color-output: #9E7E3E;
    --color-output-soft: rgba(158,126,62,0.07);
    --color-output-border: rgba(158,126,62,0.18);
    --color-check: #4A7C80;
    --color-check-soft: rgba(74,124,128,0.07);
    --color-check-border: rgba(74,124,128,0.16);
    /* Argument labels */
    --thesis: #B8860B;
    --premise: #2E8B57;
    --evidence: #4682B4;
    --counter: #B22222;
    --conclusion: #6A5ACD;
    /* Argument label soft backgrounds */
    --arg-thesis-bg: rgba(184,134,11,0.08);
    --arg-premise-bg: rgba(46,139,87,0.08);
    --arg-evidence-bg: rgba(70,130,180,0.08);
    --arg-counter-bg: rgba(178,34,34,0.08);
    --arg-conclusion-bg: rgba(106,90,205,0.08);
    /* Borders */
    --border: rgba(0,0,0,0.08);
    --border-strong: rgba(0,0,0,0.14);
    --divider: rgba(0,0,0,0.06);
    /* Accent */
    --accent: #3B6EA8;
    --accent-soft: rgba(59,110,168,0.08);
    --accent-warm: #B48A45;
    --accent-warm-soft: rgba(180,138,69,0.08);
    /* Shadows */
    --shadow: 0 18px 50px rgba(31,41,51,0.06);
    --shadow-sm: 0 2px 8px rgba(0,0,0,0.04);
    --shadow-soft: 0 1px 3px rgba(0,0,0,0.06);
    /* Code */
    --code-bg: rgba(0,0,0,0.04);
    --code-text: #5E6673;
    --pre-bg: #F3F0E9;
    --pre-text: #5E6673;
    /* Typography */
    --font-serif: Georgia, "Times New Roman", "Noto Serif SC", "Songti SC", serif;
    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif;
    --font-mono: "SF Mono", Menlo, Consolas, monospace;
    /* Icons */
    --icon-bg: rgba(59,110,168,0.08);
    /* Spacing */
    --section-gap: 80px;
    --card-radius: 18px;
    --card-padding: 32px 36px;
  }

  /* ── Dark Theme (toggle) ── */
  html[data-theme="dark"] {
    --bg: #070B11;
    --surface: #0B0F14;
    --card-bg: #111820;
    --card-elevated: #151D28;
    --ink: #E8EDF5;
    --ink-dim: #B8C3D4;
    --ink-muted: #7E8A9D;
    --color-context: #8FA7C8;
    --color-context-soft: rgba(143,167,200,0.10);
    --color-context-border: rgba(143,167,200,0.18);
    --color-passage: #8BA4C0;
    --color-passage-soft: rgba(139,164,192,0.10);
    --color-passage-border: rgba(139,164,192,0.18);
    --color-expression: #D4A76A;
    --color-expression-soft: rgba(212,167,106,0.10);
    --color-expression-border: rgba(212,167,106,0.20);
    --color-sentence: #C4889A;
    --color-sentence-soft: rgba(196,136,154,0.10);
    --color-sentence-border: rgba(196,136,154,0.20);
    --color-chain: #7AAA9A;
    --color-chain-soft: rgba(122,170,154,0.10);
    --color-chain-border: rgba(122,170,154,0.20);
    --color-output: #D3AA63;
    --color-output-soft: rgba(211,170,99,0.10);
    --color-output-border: rgba(211,170,99,0.20);
    --color-check: #7BA3A8;
    --color-check-soft: rgba(123,163,168,0.10);
    --color-check-border: rgba(123,163,168,0.18);
    --thesis: #F0C060;
    --premise: #78C0E0;
    --evidence: #A0D890;
    --counter: #E088A0;
    --conclusion: #D0A8F0;
    --arg-thesis-bg: rgba(240,192,96,0.12);
    --arg-premise-bg: rgba(120,192,224,0.12);
    --arg-evidence-bg: rgba(160,216,144,0.12);
    --arg-counter-bg: rgba(224,136,160,0.12);
    --arg-conclusion-bg: rgba(208,168,240,0.12);
    --border: rgba(136,157,196,0.08);
    --border-strong: rgba(136,157,196,0.15);
    --divider: rgba(136,157,196,0.06);
    --accent: #8FA7C8;
    --accent-soft: rgba(143,167,200,0.12);
    --accent-warm: #D3AA63;
    --accent-warm-soft: rgba(211,170,99,0.10);
    --shadow: 0 18px 50px rgba(0,0,0,0.35);
    --shadow-sm: 0 2px 8px rgba(0,0,0,0.20);
    --code-bg: rgba(255,255,255,0.06);
    --code-text: #B0B8C4;
    --pre-bg: rgba(0,0,0,0.25);
    --pre-text: #B0B8C4;
    --shadow-soft: 0 1px 3px rgba(0,0,0,0.25);
    --icon-bg: rgba(136,157,196,0.10);
  }

  /* ── Smooth theme transition ── */
  html { transition: background 0.3s ease; }
  body { transition: background 0.3s ease, color 0.3s ease; }
  code, pre, .mono { font-family: var(--font-mono); }
  @media (prefers-reduced-motion: reduce) {
    html, body, *, *::before, *::after { transition: none !important; }
  }

  /* ── Top Actions Bar (unified: PDF + Theme) ── */
  /* ── Fixed Top Bar (Download + Search + Theme) ── */
  .top-bar {
    position: fixed;
    top: 0; left: 0; right: 0;
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 10px;
    padding: 10px 24px;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    backdrop-filter: blur(18px) saturate(1.2);
    -webkit-backdrop-filter: blur(18px) saturate(1.2);
  }
  /* search group inside top bar */
  .tb-search {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    border: 1px solid var(--border);
    border-radius: 22px;
    background: var(--card-bg);
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
    min-width: 200px;
  }
  .tb-search:focus-within {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-soft);
  }
  .tb-search .ts-icon {
    width: 14px; height: 14px;
    color: var(--ink-muted);
    flex-shrink: 0;
  }
  .tb-search-input {
    flex: 1;
    border: none; outline: none;
    background: transparent;
    color: var(--ink);
    font-size: 13px;
    font-family: var(--font-sans);
    min-width: 80px;
  }
  .tb-search-input::placeholder {
    color: var(--ink-muted);
    font-size: 12px;
  }
  .tb-search-count {
    font-size: 11px;
    color: var(--ink-muted);
    font-family: var(--font-mono);
    white-space: nowrap;
    display: none;
  }
  .tb-search-count.visible { display: inline; }
  .tb-search-nav {
    width: 24px; height: 24px;
    display: none;
    align-items: center; justify-content: center;
    border: none; background: transparent;
    color: var(--ink-muted);
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    padding: 0;
    transition: all 0.15s ease;
  }
  .tb-search-nav.visible { display: flex; }
  .tb-search-nav:hover {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .tb-search-nav:disabled {
    opacity: 0.3; cursor: default;
  }
  .tb-search-clear {
    width: 24px; height: 24px;
    display: none;
    align-items: center; justify-content: center;
    border: none; background: transparent;
    color: var(--ink-muted);
    border-radius: 4px;
    cursor: pointer;
    font-size: 16px;
    padding: 0;
    line-height: 1;
  }
  .tb-search-clear.visible { display: flex; }
  .tb-search-clear:hover { color: var(--ink); }

  .top-action-btn {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 8px 16px;
    border-radius: 22px;
    border: 1px solid var(--border);
    background: var(--card-bg);
    color: var(--slate);
    font-size: 12px;
    font-family: var(--font-sans);
    font-weight: 500;
    letter-spacing: 0.04em;
    cursor: pointer;
    transition: all 0.25s ease;
    box-shadow: var(--shadow-sm);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    text-decoration: none;
    white-space: nowrap;
    user-select: none;
  }
  .top-action-btn:hover {
    border-color: var(--accent);
    color: var(--accent);
    box-shadow: 0 2px 8px rgba(0,0,0,0.04);
  }
  .top-action-btn:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .top-action-btn .ta-icon {
    width: 16px; height: 16px;
    display: flex; align-items: center; justify-content: center;
    color: var(--ink-dim);
  }
  .top-action-btn .ta-icon svg {
    width: 16px; height: 16px;
  }
  /* ── Text Selection Popup (划词检索) ──
     Academic paper-reading style: soft shadow, no glass blur, serif preview */
  .text-select-popup {
    position: fixed;
    z-index: 9999;
    display: flex;
    flex-direction: column;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    box-shadow: var(--shadow), 0 0 0 1px rgba(0,0,0,0.03);
    padding: 8px;
    gap: 2px;
    opacity: 0;
    transform: translateY(4px);
    transition: opacity 0.15s ease, transform 0.15s ease;
    pointer-events: none;
    user-select: none;
    min-width: 300px;
    max-width: 440px;
  }
  .text-select-popup.active {
    opacity: 1;
    transform: translateY(0);
    pointer-events: auto;
  }
  .ts-selected-preview {
    font-family: var(--font-serif);
    font-size: 11.5px;
    color: var(--ink-dim);
    line-height: 1.45;
    padding: 6px 10px;
    background: var(--card-elevated);
    border-left: 2px solid var(--accent);
    border-radius: 0 6px 6px 0;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-style: italic;
    letter-spacing: 0.01em;
    margin-bottom: 2px;
  }
  .ts-actions-row {
    display: flex;
    align-items: center;
    gap: 2px;
  }
  .text-select-popup .ts-btn {
    display: flex;
    align-items: center;
    gap: 5px;
    padding: 7px 10px;
    border: none;
    border-radius: 7px;
    background: transparent;
    color: var(--ink-dim);
    font-size: 11.5px;
    font-family: var(--font-sans);
    font-weight: 500;
    cursor: pointer;
    white-space: nowrap;
    transition: all 0.12s ease;
    letter-spacing: 0.02em;
  }
  .text-select-popup .ts-btn:hover {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .text-select-popup .ts-btn.ts-btn-disabled {
    opacity: 0.35 !important;
    cursor: not-allowed !important;
    pointer-events: none;
  }
  .text-select-popup .ts-btn.ts-btn-disabled:hover {
    background: transparent;
    color: var(--ink-dim);
  }
  .text-select-popup .ts-btn svg {
    width: 13px; height: 13px;
    stroke: currentColor;
    flex-shrink: 0;
  }
  .text-select-popup .ts-btn.ts-btn-save {
    color: var(--accent-warm);
  }
  .text-select-popup .ts-btn.ts-btn-save:hover {
    background: var(--accent-warm-soft);
    color: var(--accent-warm);
  }
  .text-select-popup .ts-divider {
    width: 1px; height: 18px;
    background: var(--border);
    margin: 0 1px;
  }

  /* ── Mobile Bottom Sheet (划词检索 · 移动端) ── */
  .selection-bs-overlay {
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.28);
    z-index: 9997;
    opacity: 0; pointer-events: none;
    transition: opacity 0.25s ease;
    -webkit-tap-highlight-color: transparent;
  }
  .selection-bs-overlay.active {
    opacity: 1; pointer-events: auto;
  }

  .selection-bottom-sheet {
    position: fixed; bottom: 0; left: 0; right: 0;
    z-index: 9998;
    background: var(--card-bg);
    border-radius: 16px 16px 0 0;
    padding: 12px 16px max(24px, env(safe-area-inset-bottom));
    box-shadow: 0 -4px 24px rgba(0,0,0,0.12);
    transform: translateY(100%);
    transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    user-select: none;
    -webkit-user-select: none;
  }
  .selection-bottom-sheet.active {
    transform: translateY(0);
  }

  .bs-drag-handle {
    width: 36px; height: 4px;
    background: var(--border-strong);
    border-radius: 2px;
    margin: 0 auto 14px;
    opacity: 0.6;
  }

  .bs-selected-text {
    font-family: var(--font-serif);
    font-size: 13px; color: var(--ink-dim);
    line-height: 1.5;
    padding: 10px 14px;
    background: var(--card-elevated);
    border-left: 3px solid var(--accent);
    border-radius: 0 10px 10px 0;
    margin-bottom: 14px;
    max-height: 60px; overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    text-overflow: ellipsis;
    font-style: italic;
  }

  .bs-actions {
    display: flex; flex-direction: column;
    gap: 4px;
  }

  .bs-action {
    display: flex; align-items: center; gap: 14px;
    padding: 14px 16px;
    border: none; border-radius: 10px;
    background: transparent;
    color: var(--ink);
    font-family: var(--font-sans);
    font-size: 14px; font-weight: 500;
    cursor: pointer;
    transition: background 0.15s ease;
    text-align: left; width: 100%;
    -webkit-tap-highlight-color: transparent;
  }
  .bs-action:active {
    background: var(--accent-soft);
  }
  .bs-action svg {
    width: 20px; height: 20px;
    stroke: var(--accent);
    flex-shrink: 0;
  }
  .bs-action.bs-action-save svg {
    stroke: var(--accent-warm);
  }
  .bs-action.bs-action-disabled {
    opacity: 0.4; pointer-events: none;
  }

  .bs-cancel {
    display: block; width: 100%; margin-top: 12px;
    padding: 12px;
    border: 1px solid var(--border);
    border-radius: 10px;
    background: transparent;
    color: var(--ink-muted);
    font-family: var(--font-sans);
    font-size: 13px; font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
    -webkit-tap-highlight-color: transparent;
  }
  .bs-cancel:active {
    background: var(--card-elevated);
    color: var(--ink-dim);
  }

  /* ── Research Lookup Drawer ── */
  .research-drawer-overlay {
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.25);
    z-index: 9997;
    opacity: 0; pointer-events: none;
    transition: opacity 0.2s ease;
  }
  .research-drawer-overlay.active {
    opacity: 1; pointer-events: auto;
  }
  .research-drawer {
    position: fixed; top: 0; right: 0; bottom: 0;
    width: 460px; max-width: 90vw;
    background: var(--bg);
    border-left: 1px solid var(--border);
    z-index: 9998;
    display: flex; flex-direction: column;
    transform: translateX(100%);
    transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
    box-shadow: -8px 0 32px rgba(0,0,0,0.3);
  }
  .research-drawer.active {
    transform: translateX(0);
  }
  .rd-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .rd-header-left {
    display: flex; align-items: center; gap: 10px;
    min-width: 0;
  }
  .rd-query-badge {
    font-size: 11px; font-weight: 600;
    letter-spacing: 0.05em; text-transform: uppercase;
    padding: 3px 8px;
    border-radius: 5px;
    background: var(--accent-soft);
    color: var(--accent);
    font-family: var(--font-mono);
    flex-shrink: 0;
  }
  .rd-title {
    font-size: 14px; font-weight: 600;
    color: var(--ink);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    font-family: var(--font-sans);
  }
  .rd-close {
    width: 32px; height: 32px;
    border: none; background: transparent;
    color: var(--ink-muted);
    cursor: pointer;
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-size: 18px;
    transition: all 0.12s ease;
    flex-shrink: 0;
  }
  .rd-close:hover {
    background: var(--card-bg);
    color: var(--ink);
  }
  .rd-body {
    flex: 1; overflow-y: auto; padding: 12px 18px 24px;
    display: flex; flex-direction: column; gap: 10px;
  }
  .rd-body::-webkit-scrollbar { width: 4px; }
  .rd-body::-webkit-scrollbar-thumb {
    background: var(--border); border-radius: 2px;
  }
  .rd-loading {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    padding: 60px 0; gap: 12px;
    color: var(--ink-muted);
    font-size: 13px;
    font-family: var(--font-sans);
  }
  .rd-spinner {
    width: 28px; height: 28px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: rd-spin 0.7s linear infinite;
  }
  @keyframes rd-spin { to { transform: rotate(360deg); } }
  .rd-empty {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    padding: 60px 0; gap: 8px;
    color: var(--ink-muted);
    font-size: 13px;
    font-family: var(--font-sans);
    text-align: center;
  }
  .rd-empty-icon {
    font-size: 32px; opacity: 0.4; margin-bottom: 4px;
  }
  .rd-result-card {
    padding: 12px 14px;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: var(--card-bg);
    cursor: pointer;
    transition: all 0.12s ease;
    text-decoration: none;
    display: block;
  }
  .rd-result-card:hover {
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent-soft);
  }
  .rd-result-meta {
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 6px;
  }
  .rd-section-tag {
    font-size: 9px; font-weight: 700;
    letter-spacing: 0.06em; text-transform: uppercase;
    padding: 2px 7px; border-radius: 4px;
    font-family: var(--font-mono);
    flex-shrink: 0;
  }
  .rd-section-tag.tag-context { background: rgba(74,111,165,0.15); color: #729ed4; }
  .rd-section-tag.tag-passage { background: rgba(61,106,158,0.15); color: #6e98d4; }
  .rd-section-tag.tag-expressions { background: rgba(166,124,46,0.15); color: #d4a84c; }
  .rd-section-tag.tag-sentence { background: rgba(158,86,112,0.15); color: #d47a96; }
  .rd-section-tag.tag-argument_chain { background: rgba(58,125,106,0.15); color: #6ec9a8; }
  .rd-section-tag.tag-output { background: rgba(158,126,62,0.15); color: #d4b06e; }
  .rd-section-tag.tag-topic { background: rgba(129,140,248,0.12); color: #a5adf0; }
  .rd-section-tag.tag-web { background: rgba(108,199,145,0.12); color: #8ad4a8; }
  .rd-date {
    font-size: 11px; color: var(--ink-muted);
    font-family: var(--font-mono);
  }
  .rd-snippet {
    font-size: 12.5px; line-height: 1.55;
    color: var(--ink-dim);
    font-family: var(--font-sans);
    display: -webkit-box;
    -webkit-line-clamp: 4;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .rd-snippet em {
    font-weight: 600; color: var(--accent);
    font-style: normal;
    background: rgba(136,157,196,0.15);
    padding: 0 2px; border-radius: 2px;
  }
  .rd-web-url {
    font-size: 10px; color: var(--ink-muted);
    margin-top: 4px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    font-family: var(--font-mono);
  }
  .rd-mode-switch {
    display: flex; gap: 4px;
    padding: 4px;
    border-radius: 9px;
    background: var(--card-bg);
    border: 1px solid var(--border);
    margin-bottom: 4px;
  }
  .rd-mode-btn {
    flex: 1;
    padding: 7px 8px;
    border: none; border-radius: 7px;
    background: transparent;
    color: var(--ink-muted);
    font-size: 11.5px; font-weight: 600;
    font-family: var(--font-sans);
    letter-spacing: 0.02em;
    cursor: pointer;
    transition: all 0.12s ease;
    white-space: nowrap;
  }
  .rd-mode-btn.active {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .rd-mode-btn:hover:not(.active) {
    color: var(--ink-dim);
  }
  .rd-mode-btn.rd-mode-disabled {
    cursor: not-allowed !important;
  }
  .rd-external-link {
    border-color: rgba(245,158,11,0.25) !important;
    background: rgba(245,158,11,0.04) !important;
  }
  .rd-external-link:hover {
    border-color: rgba(245,158,11,0.5) !important;
    box-shadow: 0 0 0 2px rgba(245,158,11,0.12) !important;
  }

  /* ── In-page search highlights ── */
  .rd-highlight {
    background: rgba(255, 200, 50, 0.38);
    color: inherit;
    border-radius: 2px;
    padding: 0 1px;
    transition: background 0.15s ease;
  }
  .rd-highlight.rd-highlight-active {
    background: rgba(255, 170, 0, 0.75);
    outline: 2px solid rgba(255, 140, 0, 0.85);
    outline-offset: 1px;
    border-radius: 3px;
    scroll-margin-top: 120px;
    scroll-margin-bottom: 120px;
  }

  /* ── In-page nav bar ── */
  .rd-nav-bar {
    display: flex; align-items: center; gap: 9px;
    padding: 10px 0;
    border-bottom: 1px solid var(--border);
    margin-bottom: 2px;
  }
  .rd-nav-count {
    font-size: 12.5px; font-weight: 600;
    color: var(--ink);
    font-family: var(--font-sans);
    flex: 1;
  }
  .rd-nav-btn {
    width: 30px; height: 30px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--card-bg);
    color: var(--ink-dim);
    cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: all 0.12s ease;
    flex-shrink: 0;
  }
  .rd-nav-btn:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
    background: var(--accent-soft);
  }
  .rd-nav-btn:disabled {
    opacity: 0.3; cursor: default;
  }
  .rd-nav-btn svg { width: 14px; height: 14px; }

  /* ── In-page match cards ── */
  .rd-match-card {
    padding: 11px 13px;
    border-radius: 9px;
    border: 1px solid var(--border);
    background: var(--card-bg);
    cursor: pointer;
    transition: all 0.12s ease;
  }
  .rd-match-card:hover {
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent-soft);
  }
  .rd-match-card.active {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  .rd-match-index {
    font-size: 10px; font-weight: 700;
    color: var(--accent);
    font-family: var(--font-mono);
    margin-bottom: 4px;
  }
  .rd-match-context {
    font-size: 12.5px; line-height: 1.55;
    color: var(--ink-dim);
    font-family: var(--font-sans);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .rd-match-context em {
    font-weight: 600; color: var(--accent);
    font-style: normal;
    background: rgba(136,157,196,0.15);
    padding: 0 2px; border-radius: 2px;
  }

  /* ── Explain Card in Drawer ── */
  .rd-explain-card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    margin-top: 10px;
  }
  .rd-ex-section {
    margin-bottom: 16px;
  }
  .rd-ex-section:last-child {
    margin-bottom: 12px;
  }
  .rd-ex-label {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--accent);
    margin-bottom: 6px;
    font-family: var(--font-sans);
  }
  .rd-ex-text {
    font-size: 13px;
    color: var(--ink);
    line-height: 1.65;
  }
  .rd-ex-fn {
    font-style: italic;
    color: var(--ink-accent, var(--ink-dim));
  }
  .rd-ex-example {
    background: var(--card-bg);
    border-left: 3px solid var(--accent);
    padding: 10px 14px;
    border-radius: 0 8px 8px 0;
  }
  .rd-ex-pattern {
    font-size: 12.5px;
    font-family: var(--font-mono);
    color: var(--ink);
    line-height: 1.7;
    padding: 6px 8px;
    margin-bottom: 2px;
    display: flex;
    align-items: baseline;
    gap: 6px;
    position: relative;
    border-radius: 4px;
    transition: background 0.15s ease;
  }
  .rd-ex-pattern:hover {
    background: var(--card-bg);
  }
  .rd-ex-bullet {
    color: var(--accent);
    flex-shrink: 0;
    font-weight: 700;
  }
  .rd-ex-copy-btn {
    flex-shrink: 0;
    width: 24px; height: 24px;
    border: none; border-radius: 4px;
    background: transparent;
    color: var(--ink-muted);
    cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    margin-left: auto;
    transition: all 0.15s ease;
    opacity: 0;
  }
  .rd-ex-pattern:hover .rd-ex-copy-btn {
    opacity: 1;
  }
  .rd-ex-copy-btn:hover {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .rd-ex-copy-btn svg {
    width: 13px; height: 13px;
    fill: none; stroke: currentColor;
  }
  .rd-ex-source-tag {
    font-size: 10px;
    color: var(--ink-muted);
    font-family: var(--font-mono);
    text-align: right;
    margin-top: 4px;
  }

  @media (max-width: 640px) {
    .research-drawer {
      width: 100vw; max-width: 100vw;
    }
    .text-select-popup {
      display: none !important; /* mobile uses bottom sheet instead */
    }
    .top-bar { padding: 6px 10px; gap: 6px; }
    .tb-search { min-width: 120px; padding: 4px 10px; }
    .tb-search-input { font-size: 12px; }
    .top-action-btn { padding: 6px 10px; font-size: 11px; }
    .top-action-btn .ta-label { display: none; }
  }
  @media (min-width: 641px) {
    .selection-bottom-sheet, .selection-bs-overlay {
      display: none !important; /* desktop uses floating popup instead */
    }
  }

  /* ── Reset & Base ── */
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html { scroll-behavior: smooth; }
  body {
    font-family: var(--font-sans);
    background: var(--bg);
    color: var(--ink);
    line-height: 1.75;
    font-size: 15px;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }

  /* ── Icon Mark (SVG icon component) ── */
  .icon-mark {
    width: 38px; height: 38px;
    border-radius: 12px;
    display: inline-flex;
    align-items: center; justify-content: center;
    background: var(--icon-bg);
    border: 1px solid var(--border);
    color: var(--accent);
    flex-shrink: 0;
  }
  .icon-mark svg {
    width: 20px; height: 20px;
    stroke: currentColor;
    stroke-width: 1.7;
    fill: none;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  /* ── Layout ── */
  .issue-shell {
    max-width: 1200px;
    margin: 0 auto;
    padding: 80px 40px 100px;
    display: grid;
    grid-template-columns: 220px minmax(0, 800px);
    gap: 80px;
    align-items: start;
  }
  .issue-toc {
    position: sticky;
    top: 96px;
    align-self: start;
    max-height: calc(100vh - 120px);
    overflow-y: auto;
  }
  .issue-main {
    width: 100%;
    max-width: 800px;
    margin: 0 auto;
  }

  /* Mobile: stack layout */
  @media (max-width: 860px) {
    .issue-shell {
      display: block;
      padding: 60px 20px 56px;
    }
    .issue-toc {
      position: static;
      max-height: none;
      overflow: visible;
      margin-bottom: 40px;
    }
    .issue-main {
      max-width: 100%;
    }
  }

  /* ── Sticky TOC (Desktop) ── */
  .issue-toc {
    color: var(--ink-muted);
    font-size: 13px;
  }
  .toc-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 24px;
    transition: margin 0.3s ease;
  }
  .toc-label {
    font-size: 11px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--ink-muted);
    font-family: var(--font-sans);
  }
  .toc-toggle {
    background: none;
    border: 1px solid var(--border-strong);
    border-radius: 6px;
    color: var(--ink-muted);
    cursor: pointer;
    padding: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: color 0.2s ease, border-color 0.2s ease;
    flex-shrink: 0;
  }
  .toc-toggle:hover {
    color: var(--ink-dim);
    border-color: var(--ink-dim);
  }
  .toc-toggle svg {
    display: block;
    transition: transform 0.3s ease;
  }
  /* Collapsed TOC */
  .issue-shell.toc-collapsed {
    grid-template-columns: 48px minmax(0, 800px);
  }
  .issue-toc.collapsed {
    overflow: hidden;
  }
  .issue-toc.collapsed .toc-list {
    display: none;
  }
  .issue-toc.collapsed .toc-label {
    display: none;
  }
  .issue-toc.collapsed .toc-header {
    margin-bottom: 0;
    justify-content: center;
  }
  .issue-toc.collapsed .toc-toggle svg {
    transform: rotate(180deg);
  }
  .toc-list { list-style: none; padding: 0; }
  .toc-link {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 9px 0;
    color: var(--ink-muted);
    text-decoration: none;
    transition: color .2s ease, transform .2s ease;
    font-family: var(--font-sans);
    font-size: 12.5px;
    font-weight: 500;
    letter-spacing: 0.02em;
    line-height: 1.4;
  }
  .toc-num {
    font-size: 10.5px;
    font-weight: 700;
    font-family: var(--font-mono);
    color: inherit;
    opacity: 0.5;
  }
  .toc-link::before {
    content: "";
    width: 12px;
    height: 1px;
    background: var(--border-strong);
    flex-shrink: 0;
  }
  .toc-link:hover { color: var(--ink); transform: translateX(2px); }
  .toc-link.active { color: var(--ink); }
  .toc-link.active::before {
    background: var(--accent);
    width: 22px;
  }
  .toc-link.toc-context.active { color: var(--color-context); }
  .toc-link.toc-context.active::before { background: var(--color-context); width: 22px; }
  .toc-link.toc-passage.active { color: var(--color-passage); }
  .toc-link.toc-passage.active::before { background: var(--color-passage); width: 22px; }
  .toc-link.toc-expression.active { color: var(--color-expression); }
  .toc-link.toc-expression.active::before { background: var(--color-expression); width: 22px; }
  .toc-link.toc-sentence.active { color: var(--color-sentence); }
  .toc-link.toc-sentence.active::before { background: var(--color-sentence); width: 22px; }
  .toc-link.toc-chain.active { color: var(--color-chain); }
  .toc-link.toc-chain.active::before { background: var(--color-chain); width: 22px; }
  .toc-link.toc-output.active { color: var(--color-output); }
  .toc-link.toc-output.active::before { background: var(--color-output); width: 22px; }

  /* Mobile TOC (horizontal scroll) */
  .toc-mobile {
    display: none;
    position: sticky;
    top: 0;
    z-index: 100;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    padding: 10px 16px;
    overflow-x: auto;
    white-space: nowrap;
    -webkit-overflow-scrolling: touch;
  }
  .toc-mobile::-webkit-scrollbar { display: none; }
  .toc-mobile .toc-chip {
    display: inline-block;
    padding: 6px 14px;
    margin-right: 8px;
    border-radius: 20px;
    font-size: 12px;
    font-family: var(--font-sans);
    color: var(--ink-muted);
    text-decoration: none;
    border: 1px solid var(--border);
    transition: all 0.2s ease;
  }
  .toc-mobile .toc-chip.active { color: var(--ink); border-color: var(--border-strong); background: var(--card-bg); }
  .toc-mobile .toc-chip.active.toc-context { background: var(--color-context-soft); border-color: var(--color-context-border); color: var(--color-context); }
  .toc-mobile .toc-chip.active.toc-passage { background: var(--color-passage-soft); border-color: var(--color-passage-border); color: var(--color-passage); }
  .toc-mobile .toc-chip.active.toc-expression { background: var(--color-expression-soft); border-color: var(--color-expression-border); color: var(--color-expression); }
  .toc-mobile .toc-chip.active.toc-sentence { background: var(--color-sentence-soft); border-color: var(--color-sentence-border); color: var(--color-sentence); }
  .toc-mobile .toc-chip.active.toc-chain { background: var(--color-chain-soft); border-color: var(--color-chain-border); color: var(--color-chain); }
  .toc-mobile .toc-chip.active.toc-output { background: var(--color-output-soft); border-color: var(--color-output-border); color: var(--color-output); }

  @media (max-width: 860px) {
    .issue-toc { display: none; }
    .toc-mobile { display: block; }
  }

  /* ── Hero ── */
  .issue-hero {
    text-align: center;
    padding: 56px 0 64px;
    margin-bottom: 48px;
    position: relative;
  }
  .issue-hero::after {
    content: '';
    position: absolute;
    bottom: 0;
    left: 50%;
    transform: translateX(-50%);
    width: 120px;
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--border-strong), transparent);
  }
  .issue-kicker {
    font-size: 10.5px;
    font-weight: 600;
    letter-spacing: 1.8px;
    color: var(--accent);
    margin-bottom: 22px;
    font-family: var(--font-sans);
    text-transform: uppercase;
  }
  .issue-hero h1 {
    font-family: var(--font-serif);
    font-size: clamp(36px, 5.2vw, 58px);
    line-height: 1.06;
    letter-spacing: -0.04em;
    color: var(--ink);
    margin: 0 0 20px;
    font-weight: 700;
  }
  .issue-title {
    font-family: var(--font-serif);
    font-size: 18px;
    line-height: 1.6;
    color: var(--ink-dim);
    font-style: italic;
    margin: 0 auto 16px;
    max-width: 720px;
  }
  .issue-meta {
    font-size: 13.5px;
    line-height: 1.6;
    color: var(--ink-muted);
    margin: 0;
    font-family: var(--font-sans);
  }

  /* ── Issue Sections ── */
  .issue-section {
    margin-bottom: var(--section-gap);
    scroll-margin-top: 60px;
    padding: 8px 0;
  }
  .issue-section:last-of-type { margin-bottom: 48px; }

  /* Section Heading with color badge */
  .section-heading {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 32px;
    padding-bottom: 18px;
    border-bottom: 1px solid var(--divider);
  }
  .section-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border-radius: 12px;
    font-size: 15px;
    font-weight: 700;
    font-family: var(--font-sans);
    flex-shrink: 0;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }
  .section-heading h2 {
    font-size: 22px;
    font-weight: 700;
    font-family: var(--font-serif);
    letter-spacing: -0.3px;
    line-height: 1.25;
  }
  .section-heading .section-subtitle {
    font-size: 13px;
    color: var(--ink-muted);
    font-weight: 400;
    font-style: italic;
    margin-left: 4px;
  }

  /* Module-specific badge + heading colors */
  .section-context .section-badge { background: var(--color-context-soft); color: var(--color-context); }
  .section-context .section-heading h2 { color: var(--color-context); }
  .section-passage .section-badge { background: var(--color-passage-soft); color: var(--color-passage); }
  .section-passage .section-heading h2 { color: var(--color-passage); }
  .section-expression .section-badge { background: var(--color-expression-soft); color: var(--color-expression); }
  .section-expression .section-heading h2 { color: var(--color-expression); }
  .section-sentence .section-badge { background: var(--color-sentence-soft); color: var(--color-sentence); }
  .section-sentence .section-heading h2 { color: var(--color-sentence); }
  .section-chain .section-badge { background: var(--color-chain-soft); color: var(--color-chain); }
  .section-chain .section-heading h2 { color: var(--color-chain); }
  .section-output .section-badge { background: var(--color-output-soft); color: var(--color-output); }
  .section-output .section-heading h2 { color: var(--color-output); }

  /* ── Source Badges ── */
  .source-badge-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 10px 0 18px;
  }
  .source-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 9px;
    border-radius: 999px;
    font-size: 11px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    font-weight: 700;
    font-family: var(--font-sans);
    border: 1px solid var(--border-strong);
    background: var(--card-bg);
    color: var(--ink-muted);
  }
  .source-badge.source {
    color: #3B6EA8;
    border-color: rgba(59,110,168,0.26);
    background: rgba(59,110,168,0.07);
  }
  html[data-theme="dark"] .source-badge.source {
    color: #93C5FD;
    border-color: rgba(147,197,253,0.26);
    background: rgba(59,130,246,0.08);
  }
  .source-badge.training {
    color: #A67C2E;
    border-color: rgba(166,124,46,0.26);
    background: rgba(166,124,46,0.07);
  }
  html[data-theme="dark"] .source-badge.training {
    color: #F0C987;
    border-color: rgba(240,201,135,0.26);
    background: rgba(245,158,11,0.08);
  }
  .source-badge.ai {
    color: #6A5ACD;
    border-color: rgba(106,90,205,0.22);
    background: rgba(106,90,205,0.06);
  }
  html[data-theme="dark"] .source-badge.ai {
    color: #C4B5FD;
    border-color: rgba(196,181,253,0.26);
    background: rgba(139,92,246,0.08);
  }
  .source-badge.practice {
    color: #3A7D6A;
    border-color: rgba(58,125,106,0.24);
    background: rgba(58,125,106,0.07);
  }
  html[data-theme="dark"] .source-badge.practice {
    color: #86EFAC;
    border-color: rgba(134,239,172,0.24);
    background: rgba(34,197,94,0.08);
  }
  .source-note {
    font-size: 13px;
    line-height: 1.7;
    color: var(--ink-muted);
    margin-top: -6px;
    margin-bottom: 18px;
  }

  /* ── Source List (end of page) ── */
  .source-list {
    margin-top: 18px;
    padding: 14px 16px;
    border: 1px solid var(--border);
    background: var(--surface);
    border-radius: 12px;
  }
  .source-list-title {
    font-size: 11px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--accent);
    margin-bottom: 8px;
    font-family: var(--font-sans);
  }
  .source-list ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .source-list li {
    font-size: 13px;
    line-height: 1.65;
    color: var(--ink-dim);
    padding: 3px 0;
  }

  /* ── Content Cards ── */
  .content-card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--card-radius);
    padding: var(--card-padding);
    margin-bottom: 24px;
    box-shadow: var(--shadow-sm);
    transition: box-shadow 0.2s ease;
  }
  .content-card:hover {
    box-shadow: 0 2px 12px rgba(0,0,0,0.04);
  }
  .content-card:last-child { margin-bottom: 0; }
  .content-card > * + * { margin-top: 16px; }

  /* Card with left accent border */
  .content-card.accent-left {
    border-left: 3px solid transparent;
  }
  .section-context .content-card.accent-left { border-left-color: var(--color-context-border); }
  .section-passage .content-card.accent-left { border-left-color: var(--color-passage-border); }
  .section-expression .content-card.accent-left { border-left-color: var(--color-expression-border); }
  .section-sentence .content-card.accent-left { border-left-color: var(--color-sentence-border); }
  .section-chain .content-card.accent-left { border-left-color: var(--color-chain-border); }
  .section-output .content-card.accent-left { border-left-color: var(--color-output-border); }

  /* ── Context Blocks (Pane 1 sub-sections) ── */
  .ctx-block {
    background: var(--surface-elevated);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 24px 28px;
    margin-bottom: 20px;
    box-shadow: var(--shadow-sm);
    transition: box-shadow 0.2s ease, border-color 0.2s ease;
  }
  .ctx-block:hover {
    box-shadow: 0 2px 12px rgba(0,0,0,0.04);
    border-color: var(--border-hover);
  }
  .ctx-block:last-child { margin-bottom: 0; }
  .ctx-label {
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--color-context);
    margin-bottom: 10px;
    font-family: var(--font-sans);
  }
  .ctx-block .ctx-text {
    font-size: 15px;
    color: var(--ink-dim);
    line-height: 1.8;
  }
  .ctx-block .ctx-text strong { color: var(--ink); font-weight: 700; }
  /* Framing list */
  .ctx-block .framing-list {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .ctx-block .framing-list li {
    font-size: 15px;
    color: var(--ink-dim);
    line-height: 1.8;
    padding: 8px 0 8px 18px;
    border-left: 2px solid var(--color-context-border);
    margin-bottom: 8px;
  }
  .ctx-block .framing-list li:last-child { margin-bottom: 0; }
  .ctx-block .framing-list li strong { color: var(--ink); font-weight: 700; }
  /* Context block bullet lists (debate, etc.) */
  .ctx-block .ctx-list {
    margin: 12px 0 12px 0;
    padding: 0;
    list-style: none;
  }
  .ctx-block .ctx-list li {
    font-size: 15px;
    color: var(--ink-dim);
    line-height: 1.8;
    padding: 8px 0 8px 18px;
    border-left: 2px solid var(--color-context-border);
    margin-bottom: 8px;
  }
  .ctx-block .ctx-list li:last-child { margin-bottom: 0; }
  .ctx-block .ctx-list li strong { color: var(--ink); font-weight: 700; }
  /* Context block accent borders */
  .ctx-block.ctx-topic { border-left: 3px solid var(--color-context); }
  .ctx-block.ctx-bg { border-left: 3px solid var(--color-context-muted, #6a8ec2); }
  .ctx-block.ctx-debate { border-left: 3px solid var(--color-arg-counter, #b22222); }
  .ctx-block.ctx-rationale { border-left: 3px solid var(--color-chain, #3a7d6a); }
  .ctx-block.ctx-framing { border-left: 3px solid var(--color-output, #9e7e3e); }

  /* ── Typography ── */
  p {
    margin-bottom: 16px;
    line-height: 1.85;
    max-width: 760px;
  }
  p:last-child { margin-bottom: 0; }
  .content-card p { max-width: none; }

  .cn-body {
    font-size: 16px;
    color: var(--ink-dim);
    line-height: 1.85;
    margin-bottom: 16px;
  }
  .cn-body strong { color: var(--ink); font-weight: 700; }
  .en-body {
    font-size: 16px;
    color: var(--ink);
    line-height: 1.8;
    margin-bottom: 16px;
    font-family: Georgia, 'Times New Roman', 'Noto Serif SC', 'PingFang SC', serif;
  }
  .en-body strong { color: var(--ink); font-weight: 700; }

  /* Long paragraph handling */
  .cn-body.long, .en-body.long {
    line-height: 1.9;
  }

  /* ── Subtitle / Meta lines ── */
  .section-subtitle {
    font-size: 13px;
    color: var(--ink-muted);
    font-style: italic;
    margin-bottom: 12px;
  }
  .source-line {
    font-size: 12px;
    color: var(--ink-muted);
    font-style: italic;
    margin-bottom: 16px;
    font-family: var(--font-sans);
  }

  /* ── Passage Block ── */
  .passage-block {
    background: var(--card-elevated);
    border: 1px solid var(--color-passage-border);
    border-radius: var(--card-radius);
    padding: 32px 36px;
    margin-bottom: 0;
    border-left: 3px solid var(--color-passage);
    box-shadow: var(--shadow-sm);
    transition: box-shadow 0.2s ease;
  }
  .passage-block:hover {
    box-shadow: 0 2px 12px rgba(0,0,0,0.04);
  }
  .passage-block p {
    font-size: 15.5px;
    line-height: 1.85;
    color: var(--ink);
    max-width: 780px;
  }
  .passage-block .source-line {
    color: var(--ink-muted);
    margin-bottom: 18px;
  }

  /* Argument labels */
  .arg-label {
    display: inline-block;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 4px;
    margin-right: 5px;
    font-family: var(--font-sans);
    letter-spacing: 0.3px;
    text-transform: uppercase;
    vertical-align: middle;
    position: relative;
    top: -1px;
  }
  .arg-thesis { background: var(--arg-thesis-bg); color: var(--thesis); }
  .arg-premise { background: var(--arg-premise-bg); color: var(--premise); }
  .arg-evidence { background: var(--arg-evidence-bg); color: var(--evidence); }
  .arg-counter { background: var(--arg-counter-bg); color: var(--counter); }
  .arg-conclusion { background: var(--arg-conclusion-bg); color: var(--conclusion); }

  /* ── Reading Guide (callout) ── */
  .guide-block {
    background: var(--color-passage-soft);
    border-left: 3px solid var(--color-passage);
    padding: 20px 26px;
    border-radius: 0 12px 12px 0;
    margin-bottom: 0;
    font-size: 14px;
    color: var(--ink-dim);
    line-height: 1.75;
  }
  .guide-block strong { color: var(--color-passage); }

  /* ── Expression Cards ── */
  .expr-card {
    background: var(--card-bg);
    border: 1px solid var(--color-expression-border);
    border-radius: var(--card-radius);
    padding: 28px 32px;
    margin-bottom: 20px;
    border-left: 3px solid var(--color-expression);
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
    box-shadow: var(--shadow-sm);
  }
  .expr-card:hover { border-color: var(--color-expression); box-shadow: 0 2px 12px rgba(0,0,0,0.04); }
  .expr-card:last-child { margin-bottom: 0; }
  .expr-card .expr-num {
    font-size: 10.5px;
    font-weight: 600;
    color: var(--color-expression);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 6px;
    font-family: var(--font-sans);
  }
  .expr-card .expr-phrase {
    font-size: 18px;
    font-weight: 700;
    color: var(--ink);
    font-family: var(--font-mono);
    margin-bottom: 8px;
    letter-spacing: -0.2px;
  }
  .expr-card .expr-tags {
    font-size: 11px;
    color: var(--color-expression);
    margin-bottom: 12px;
    font-family: var(--font-sans);
    font-weight: 500;
    letter-spacing: 0.3px;
  }
  .expr-card .expr-cn {
    font-size: 14px;
    color: var(--ink-dim);
    margin-bottom: 8px;
    line-height: 1.7;
  }
  .expr-card .expr-colloc {
    font-size: 13px;
    color: var(--ink-muted);
    margin-bottom: 6px;
    line-height: 1.65;
  }
  .expr-card .expr-example {
    font-size: 14px;
    color: var(--ink);
    font-style: italic;
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--border);
    line-height: 1.75;
  }
  .expr-card .expr-example em { color: var(--color-expression); font-style: normal; }

  /* ── Sentence Deconstruction ── */
  .sentence-decon {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  /* Target sentence quote card */
  .target-sentence-card {
    background: var(--card-elevated);
    border: 1px solid var(--color-sentence-border);
    border-radius: var(--card-radius);
    padding: 28px 32px;
    border-left: 3px solid var(--color-sentence);
    box-shadow: var(--shadow-sm);
    transition: box-shadow 0.2s ease;
  }
  .target-sentence-card:hover {
    box-shadow: 0 2px 12px rgba(0,0,0,0.04);
  }
  .target-sentence-card .ts-label {
    font-size: 10.5px;
    font-weight: 600;
    color: var(--color-sentence);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 10px;
    font-family: var(--font-sans);
  }
  .target-sentence-card .ts-text {
    font-size: 17px;
    line-height: 1.85;
    color: var(--ink);
    font-style: italic;
  }

  /* Why this sentence works */
  .why-card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--card-radius);
    padding: 24px 28px;
    box-shadow: var(--shadow-sm);
    transition: box-shadow 0.2s ease;
  }
  .why-card:hover {
    box-shadow: 0 2px 12px rgba(0,0,0,0.04);
  }
  .why-card .why-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-sentence);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 8px;
    font-family: var(--font-sans);
  }
  .why-card .why-text {
    font-size: 14px;
    color: var(--ink-dim);
    line-height: 1.75;
  }

  /* Structure breakdown card */
  .structure-card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--card-radius);
    padding: 24px 28px;
    box-shadow: var(--shadow-sm);
    transition: box-shadow 0.2s ease;
  }
  .structure-card:hover {
    box-shadow: 0 2px 12px rgba(0,0,0,0.04);
  }
  .structure-card .struct-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-sentence);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 8px;
    font-family: var(--font-sans);
  }
  .structure-card .struct-text {
    font-size: 14px;
    color: var(--ink-dim);
    line-height: 1.75;
  }

  /* Grammar points container */
  .grammar-section {
    display: flex;
    flex-direction: column;
    gap: 0;
  }
  .grammar-section-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-sentence);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 12px;
    font-family: var(--font-sans);
  }

  /* Grammar mini-cards */
  .grammar-mini-card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 14px;
    border-left: 3px solid var(--color-sentence);
    box-shadow: var(--shadow-sm);
    transition: box-shadow 0.2s ease;
  }
  .grammar-mini-card:hover {
    box-shadow: 0 2px 12px rgba(0,0,0,0.04);
  }
  .grammar-mini-card:last-child { margin-bottom: 0; }
  .grammar-mini-card .gm-title {
    font-size: 13px;
    font-weight: 700;
    color: var(--ink);
    margin-bottom: 6px;
    font-family: var(--font-sans);
  }
  .grammar-mini-card .gm-body {
    font-size: 14px;
    color: var(--ink-dim);
    line-height: 1.7;
  }
  .grammar-mini-card .gm-code {
    display: block;
    font-family: var(--font-mono);
    font-size: 13px;
    background: var(--pre-bg);
    padding: 10px 14px;
    border-radius: 6px;
    color: var(--pre-text);
    margin-top: 8px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
    line-height: 1.6;
  }

  /* Template block */
  .template-card {
    background: var(--card-bg);
    border: 1px solid var(--color-sentence-border);
    border-radius: var(--card-radius);
    padding: 20px 24px;
    border-left: 3px solid var(--color-sentence);
    box-shadow: var(--shadow-sm);
  }
  .template-card .tpl-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-sentence);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 10px;
    font-family: var(--font-sans);
  }
  .template-card .tpl-code {
    font-family: var(--font-mono);
    font-size: 13px;
    background: var(--pre-bg);
    padding: 14px 18px;
    border-radius: 8px;
    color: var(--pre-text);
    line-height: 1.7;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  /* Imitation example */
  .imitation-card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--card-radius);
    padding: 20px 24px;
    box-shadow: var(--shadow-sm);
  }
  .imitation-card .imit-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-sentence);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 8px;
    font-family: var(--font-sans);
  }
  .imitation-card .imit-text {
    font-size: 15px;
    color: var(--ink);
    font-style: italic;
    line-height: 1.8;
  }

  /* Scenario tag */
  .scenario-tag {
    display: inline-block;
    font-size: 12px;
    color: var(--color-sentence);
    background: var(--color-sentence-soft);
    padding: 4px 12px;
    border-radius: 6px;
    font-family: var(--font-sans);
    margin-top: 8px;
  }

  /* ── Argument Chain ── */
  .chain-flow {
    display: flex;
    flex-direction: column;
    gap: 18px;
  }
  .chain-step {
    background: var(--card-bg);
    border: 1px solid var(--color-chain-border);
    border-radius: var(--card-radius);
    padding: 24px 28px;
    border-left: 3px solid var(--color-chain);
    box-shadow: var(--shadow-sm);
    transition: box-shadow 0.2s ease;
  }
  .chain-step:hover {
    box-shadow: 0 2px 12px rgba(0,0,0,0.04);
  }
  .chain-step .step-label {
    font-size: 10.5px;
    font-weight: 600;
    color: var(--color-chain);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 10px;
    font-family: var(--font-sans);
  }
  .chain-step .step-en {
    font-size: 16px;
    color: var(--ink);
    font-weight: 600;
    line-height: 1.75;
  }
  .chain-step .step-cn {
    font-size: 15px;
    color: var(--ink-dim);
    margin-top: 6px;
    line-height: 1.75;
  }
  .chain-step .step-code {
    display: block;
    font-family: var(--font-mono);
    font-size: 12px;
    background: var(--pre-bg);
    padding: 12px 16px;
    border-radius: 6px;
    color: var(--pre-text);
    margin-top: 8px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
    line-height: 1.6;
  }

  /* Weighing card (longer reading) */
  .weighing-card {
    background: var(--card-bg);
    border: 1px solid var(--color-chain-border);
    border-radius: var(--card-radius);
    padding: 24px 28px;
    border-left: 3px solid var(--color-chain);
    box-shadow: var(--shadow-sm);
  }
  .weighing-card .weigh-label {
    font-size: 11px;
    font-weight: 700;
    color: var(--color-chain);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 12px;
    font-family: var(--font-sans);
  }
  .weighing-card .weigh-text {
    font-size: 15px;
    color: var(--ink-dim);
    line-height: 1.9;
  }
  .weighing-card .weigh-text p { margin-bottom: 16px; }
  .weighing-card .weigh-text p:last-child { margin-bottom: 0; }

  /* Sample paragraph */
  .sample-paragraph-card {
    background: var(--card-elevated);
    border: 1px solid var(--color-chain-border);
    border-radius: var(--card-radius);
    padding: 28px 32px;
    border-left: 3px solid var(--color-chain);
    box-shadow: var(--shadow-sm);
    transition: box-shadow 0.2s ease;
  }
  .sample-paragraph-card:hover {
    box-shadow: 0 2px 12px rgba(0,0,0,0.04);
  }
  .sample-paragraph-card .sp-label {
    font-size: 11px;
    font-weight: 700;
    color: var(--color-chain);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 10px;
    font-family: var(--font-sans);
  }
  .sample-paragraph-card .sp-text {
    font-size: 15px;
    line-height: 1.95;
    color: var(--ink);
  }
  .sample-paragraph-card .sp-note {
    font-size: 12px;
    color: var(--ink-muted);
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px solid var(--border);
    font-style: italic;
  }

  /* ── Output Tasks ── */
  .task-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 24px;
  }
  .task-card {
    background: var(--card-bg);
    border: 1px solid var(--color-output-border);
    border-radius: var(--card-radius);
    padding: 24px 28px;
    border-top: 3px solid var(--color-output);
    box-shadow: var(--shadow-sm);
  }
  .task-card .task-type {
    font-size: 11px;
    font-weight: 700;
    color: var(--color-output);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 12px;
    font-family: var(--font-sans);
  }
  .task-card .task-prompt {
    font-size: 15px;
    color: var(--ink);
    line-height: 1.75;
  }
  .task-card .task-meta {
    font-size: 12px;
    color: var(--ink-muted);
    margin-top: 10px;
    font-style: italic;
  }

  /* Structure guide */
  .guide-card {
    background: var(--card-bg);
    border: 1px solid var(--color-output-border);
    border-radius: var(--card-radius);
    padding: 24px 28px;
    margin-bottom: 24px;
    box-shadow: var(--shadow-sm);
  }
  .guide-card .guide-label {
    font-size: 11px;
    font-weight: 700;
    color: var(--color-output);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 16px;
    font-family: var(--font-sans);
  }
  .guide-card .step-list {
    list-style: none;
    padding: 0;
    counter-reset: step-counter;
  }
  .guide-card .step-list li {
    counter-increment: step-counter;
    font-size: 14px;
    color: var(--ink-dim);
    padding: 10px 0 10px 36px;
    position: relative;
    line-height: 1.7;
    border-bottom: 1px solid var(--border);
  }
  .guide-card .step-list li:last-child { border-bottom: none; padding-bottom: 0; }
  .guide-card .step-list li::before {
    content: counter(step-counter);
    position: absolute;
    left: 0;
    top: 10px;
    width: 24px;
    height: 24px;
    border-radius: 6px;
    background: var(--color-output-soft);
    color: var(--color-output);
    font-size: 11px;
    font-weight: 700;
    font-family: var(--font-sans);
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .guide-card .step-list li strong { color: var(--ink); }

  /* Self-check checklist */
  .check-card {
    background: var(--card-bg);
    border: 1px solid var(--color-check-border);
    border-radius: var(--card-radius);
    padding: 24px 28px;
    border-left: 3px solid var(--color-check);
    box-shadow: var(--shadow-sm);
  }
  .check-card .check-label {
    font-size: 11px;
    font-weight: 700;
    color: var(--color-check);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 16px;
    font-family: var(--font-sans);
  }
  .check-card .checklist {
    list-style: none;
    padding: 0;
  }
  .check-card .checklist li {
    font-size: 14px;
    color: var(--ink-dim);
    padding: 12px 0 12px 32px;
    position: relative;
    line-height: 1.65;
    border-bottom: 1px solid var(--border);
  }
  .check-card .checklist li:last-child { border-bottom: none; padding-bottom: 0; }
  .check-card .checklist li::before {
    content: '';
    position: absolute;
    left: 2px;
    top: 15px;
    width: 18px;
    height: 18px;
    border: 2px solid var(--color-check-border);
    border-radius: 4px;
    background: transparent;
  }
  .check-card .checklist li strong { color: var(--ink); }

  /* ── Task Block (new two-task layout) ── */
  .task-block {
    margin-bottom: 40px;
    border: 1px solid var(--border);
    border-radius: var(--card-radius);
    padding: 32px 36px;
    background: var(--surface);
    box-shadow: var(--shadow-sm);
    transition: box-shadow 0.2s ease;
  }
  .task-block:hover {
    box-shadow: 0 2px 12px rgba(0,0,0,0.04);
  }
  .task-block .task-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
  }
  .task-block .task-header .task-type {
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--accent);
    background: var(--accent-soft);
    padding: 4px 12px;
    border-radius: 6px;
  }
  .task-block .task-header .task-meta {
    font-size: 12px;
    color: var(--ink-muted);
  }
  .task-block .task-prompt {
    font-size: 15px;
    line-height: 1.7;
    color: var(--ink);
    padding: 16px 20px;
    background: var(--card-bg);
    border-left: 3px solid var(--accent);
    border-radius: 0 8px 8px 0;
    margin-bottom: 20px;
    font-style: italic;
  }
  .task-block .guide-card,
  .task-block .check-card {
    margin-top: 16px;
  }

  /* ── Premium Hint Card ── */
  .premium-hint-card {
    margin-top: 32px;
    padding: 24px 28px;
    border: 1px solid rgba(180,138,69,0.20);
    border-radius: 12px;
    background: linear-gradient(135deg, rgba(180,138,69,0.04), rgba(180,138,69,0.01));
  }
  html[data-theme="dark"] .premium-hint-card {
    border: 1px solid rgba(255,215,0,0.15);
    background: linear-gradient(135deg, rgba(255,215,0,0.04), rgba(255,215,0,0.01));
  }
  .premium-hint-card .ph-label {
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #A67C2E;
    margin-bottom: 12px;
  }
  html[data-theme="dark"] .premium-hint-card .ph-label { color: #d4a853; }
  .premium-hint-card .ph-text {
    font-size: 13px;
    line-height: 1.65;
    color: var(--ink-muted);
  }
  .premium-hint-card .ph-text strong {
    color: var(--ink);
  }

  /* ── Lists ── */
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  li {
    font-size: 15px;
    color: var(--ink-dim);
    padding: 5px 0 5px 20px;
    position: relative;
    line-height: 1.7;
    margin-bottom: 6px;
  }
  li::before {
    content: '\2014';
    position: absolute;
    left: 0;
    color: var(--ink-muted);
  }

  /* ── Code / Template / Pre ── */
  code {
    font-family: var(--font-mono);
    background: var(--code-bg);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.9em;
    color: var(--code-text);
  }
  pre {
    font-family: var(--font-mono);
    background: var(--pre-bg);
    padding: 16px 20px;
    border-radius: 8px;
    font-size: 13px;
    line-height: 1.65;
    overflow: hidden;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
    color: var(--pre-text);
  }
  .template-box {
    font-family: var(--font-mono);
    font-size: 13px;
    background: var(--pre-bg);
    padding: 14px 18px;
    border-radius: 8px;
    color: var(--pre-text);
    line-height: 1.7;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
    margin: 0;
  }

  /* ── Section Divider ── */
  .section-divider {
    border: none;
    height: 1px;
    background: var(--divider);
    margin: 0 0 var(--section-gap) 0;
  }

  /* ── Footer ── */
  .page-footer {
    padding: 40px 0;
    text-align: center;
    border-top: 1px solid var(--divider);
    margin-top: 48px;
  }
  .page-footer .brand {
    font-family: var(--font-serif);
    font-size: 20px;
    font-weight: 700;
    color: var(--ink);
    margin-bottom: 4px;
  }
  .page-footer .brand span { color: var(--color-passage); }
  .page-footer .slogan {
    font-size: 12px;
    color: var(--ink-muted);
    font-style: italic;
  }

  /* ── Highlight / Callout / Chips ── */
  .highlight-chip {
    display: inline-block;
    font-size: 11px;
    font-weight: 600;
    padding: 3px 10px;
    border-radius: 5px;
    font-family: var(--font-sans);
    letter-spacing: 0.3px;
  }
  .highlight-chip.chip-context { background: var(--color-context-soft); color: var(--color-context); }
  .highlight-chip.chip-passage { background: var(--color-passage-soft); color: var(--color-passage); }
  .highlight-chip.chip-expression { background: var(--color-expression-soft); color: var(--color-expression); }
  .highlight-chip.chip-sentence { background: var(--color-sentence-soft); color: var(--color-sentence); }
  .highlight-chip.chip-chain { background: var(--color-chain-soft); color: var(--color-chain); }

  /* Callout box */
  .callout-box {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--card-radius);
    padding: 20px 24px;
    font-size: 14px;
    color: var(--ink-dim);
    line-height: 1.75;
    box-shadow: var(--shadow-sm);
  }
  .callout-box strong { color: var(--ink); }

  /* ── Strong / Emphasis ── */
  strong { font-weight: 700; color: var(--ink); }
  em { font-style: italic; }

  /* ── Responsive ── */
  @media (max-width: 960px) {
    .issue-toc { display: none; }
    .toc-mobile { display: block; }
    .issue-shell { padding: 80px 24px 60px; }
    .issue-main { max-width: 100%; padding: 0; }
  }

  @media (max-width: 640px) {
    .issue-shell { padding: 80px 20px 60px; }
    .issue-main { padding: 0; }
    .issue-hero { padding: 40px 0 28px; }
    .issue-hero h1 { font-size: 24px; }
    .issue-title { font-size: 14px; }
    .issue-section { margin-bottom: 56px; padding: 8px 0; }
    .section-heading { gap: 10px; }
    .section-heading h2 { font-size: 18px; }
    .section-badge { width: 34px; height: 34px; font-size: 13px; border-radius: 9px; }
    .content-card { padding: 22px 20px; }
    .passage-block { padding: 22px 20px; }
    .task-grid { grid-template-columns: 1fr; }
    .expr-card { padding: 22px 20px; }
    .chain-step { padding: 20px 20px; }
    .grammar-mini-card { padding: 16px 18px; }
    .target-sentence-card { padding: 20px 20px; }
    .target-sentence-card .ts-text { font-size: 15px; }
    .check-card { padding: 22px 20px; }
    .guide-card { padding: 22px 20px; }
    .sample-paragraph-card { padding: 22px 20px; }
    .weighing-card { padding: 22px 20px; }
    .task-block { padding: 24px 20px; }
    body { font-size: 15px; }
    .cn-body, .en-body { font-size: 15px; }
    .toc-mobile { padding: 8px 12px; }
    .toc-mobile .toc-chip { padding: 5px 12px; font-size: 11px; margin-right: 6px; }
    /* Argument Chain Demo mobile */
    .arg-demo-issue { padding: 32px 16px 48px; margin-top: 40px; }
    .adi-layout { grid-template-columns: 1fr; gap: 20px; }
    .adi-input { padding: 18px; }
    .adi-card { padding: 16px 18px; }
    .adi-textarea { min-height: 100px; font-size: 13px; }
  }

  /* ── Print (PDF) ── */
  @media print {
    :root {
      --bg: #fff; --surface: #fff; --card-bg: #fafafa; --card-elevated: #f5f5f5;
      --ink: #1a1a2e; --ink-dim: #444; --ink-muted: #777;
      --color-context: #4a6fa5; --color-context-soft: rgba(74,111,165,0.06);
      --color-context-border: rgba(74,111,165,0.15);
      --color-passage: #3d6a9e; --color-passage-soft: rgba(61,106,158,0.06);
      --color-passage-border: rgba(61,106,158,0.15);
      --color-expression: #b8860b; --color-expression-soft: rgba(184,134,11,0.06);
      --color-expression-border: rgba(184,134,11,0.15);
      --color-sentence: #a0526e; --color-sentence-soft: rgba(160,82,110,0.06);
      --color-sentence-border: rgba(160,82,110,0.15);
      --color-chain: #3a7d6a; --color-chain-soft: rgba(58,125,106,0.06);
      --color-chain-border: rgba(58,125,106,0.15);
      --color-output: #b8860b; --color-output-soft: rgba(184,134,11,0.06);
      --color-output-border: rgba(184,134,11,0.15);
      --color-check: #4a7c80; --color-check-soft: rgba(74,124,128,0.06);
      --color-check-border: rgba(74,124,128,0.15);
      --thesis: #b8860b; --premise: #2e8b57; --evidence: #4682b4;
      --counter: #b22222; --conclusion: #6a5acd;
      --border: rgba(0,0,0,0.08); --border-strong: rgba(0,0,0,0.15);
      --divider: rgba(0,0,0,0.06);
      --accent: #4a7c80;
      --section-gap: 32px;
      --shadow: none; --shadow-sm: none;
    }
    body { font-size: 11pt; }
    .issue-shell { display: block; max-width: 100%; padding: 0; }
    .issue-toc, .toc-mobile, .top-bar, .reader-toolbar, .keywords-panel, .text-select-popup, .research-drawer, .research-drawer-overlay, .selection-bottom-sheet, .selection-bs-overlay, .arg-demo-issue { display: none !important; }
    .issue-main { max-width: 100%; padding: 0; }
    .issue-hero { padding: 20px 0 16px; }
    .issue-hero h1 { font-size: 18pt; }
    .issue-title { font-size: 12pt; }
    .issue-section { margin-bottom: 28px; scroll-margin-top: 0; page-break-inside: avoid; }
    .section-heading { margin-bottom: 16px; padding-bottom: 10px; }
    .section-heading h2 { font-size: 14pt; }
    .section-badge { width: 28px; height: 28px; font-size: 11px; }
    .content-card, .expr-card, .chain-step, .grammar-mini-card,
    .target-sentence-card, .why-card, .structure-card, .template-card,
    .imitation-card, .weighing-card, .sample-paragraph-card,
    .task-card, .guide-card, .check-card, .passage-block {
      page-break-inside: avoid;
    }
    .task-grid { grid-template-columns: 1fr 1fr; }
    code { background: #f0f0f0; }
    pre, .template-box, .gm-code, .step-code, .tpl-code { background: #f0f0f0; }
  }

  /* ═══════════════════════════════════════════
     Interactive Features — Copy, Expand, Check
     ═══════════════════════════════════════════ */

  /* ── Copy Button ── */
  .copy-btn {
    position: absolute;
    top: 12px;
    right: 12px;
    width: 32px;
    height: 32px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--card-bg);
    color: var(--ink-muted);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    opacity: 0;
    transition: opacity 0.2s ease, background 0.2s ease, color 0.2s ease;
    z-index: 5;
    font-size: 14px;
    line-height: 1;
  }
  .copy-btn:hover {
    background: var(--surface);
    color: var(--ink);
    border-color: var(--border-strong);
  }
  .copy-btn.copied {
    color: #2e8b57;
    border-color: rgba(46,139,87,0.30);
    background: rgba(46,139,87,0.06);
    opacity: 1 !important;
  }
  /* Show copy button on card hover */
  .card-has-copy:hover .copy-btn,
  .card-has-copy:focus-within .copy-btn,
  .card-has-copy .copy-btn.copied {
    opacity: 1;
  }
  @media (hover: none) {
    .copy-btn { opacity: 0.6; }
  }

  /* ── Toast ── */
  .interaction-toast {
    position: fixed;
    bottom: 32px;
    left: 50%;
    transform: translateX(-50%) translateY(80px);
    background: var(--ink);
    color: var(--bg);
    padding: 10px 22px;
    border-radius: 999px;
    font-size: 13px;
    font-family: var(--font-sans);
    font-weight: 600;
    letter-spacing: 0.3px;
    opacity: 0;
    transition: transform 0.35s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.25s ease;
    z-index: 9999;
    pointer-events: none;
    box-shadow: 0 8px 32px rgba(0,0,0,0.18);
  }
  .interaction-toast.show {
    transform: translateX(-50%) translateY(0);
    opacity: 1;
  }

  /* ── Card Hover Lift ── */
  .expr-card, .passage-block, .target-sentence-card, .why-card,
  .structure-card, .grammar-mini-card, .template-card, .imitation-card,
  .chain-step, .weighing-card, .sample-paragraph-card, .task-card,
  .guide-card, .check-card {
    transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
  }
  .expr-card:hover, .passage-block:hover, .target-sentence-card:hover,
  .why-card:hover, .structure-card:hover, .grammar-mini-card:hover,
  .template-card:hover, .imitation-card:hover, .chain-step:hover,
  .weighing-card:hover, .sample-paragraph-card:hover, .task-card:hover,
  .guide-card:hover, .check-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 24px rgba(0,0,0,0.08);
  }
  html[data-theme="dark"] .expr-card:hover,
  html[data-theme="dark"] .passage-block:hover,
  html[data-theme="dark"] .target-sentence-card:hover,
  html[data-theme="dark"] .why-card:hover,
  html[data-theme="dark"] .grammar-mini-card:hover,
  html[data-theme="dark"] .template-card:hover,
  html[data-theme="dark"] .imitation-card:hover,
  html[data-theme="dark"] .chain-step:hover,
  html[data-theme="dark"] .weighing-card:hover,
  html[data-theme="dark"] .sample-paragraph-card:hover,
  html[data-theme="dark"] .task-card:hover,
  html[data-theme="dark"] .guide-card:hover,
  html[data-theme="dark"] .check-card:hover {
    box-shadow: 0 6px 28px rgba(0,0,0,0.32);
  }

  /* ── Self-Check Checklist ── */
  .check-card .checklist li {
    cursor: pointer;
    transition: color 0.2s ease, background 0.2s ease;
    border-radius: 6px;
    margin: 0 -6px;
    padding-left: 38px;
    padding-right: 6px;
  }
  .check-card .checklist li:hover {
    background: var(--color-check-soft);
  }
  .check-card .checklist li.checked {
    color: var(--ink-muted);
  }
  .check-card .checklist li.checked::before {
    background: var(--color-check);
    border-color: var(--color-check);
    content: '\2713';
    color: #fff;
    font-size: 11px;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
  }
  html[data-theme="dark"] .check-card .checklist li.checked::before {
    color: var(--bg);
  }
  .check-card .checklist li.checked::after {
    content: '';
    position: absolute;
    left: 0;
    right: 0;
    top: 50%;
    height: 1px;
    background: var(--border);
    opacity: 0.4;
  }

  /* ── Grammar Card Expand/Collapse ── */
  .grammar-mini-card {
    cursor: pointer;
    position: relative;
  }
  .grammar-mini-card .gm-toggle {
    position: absolute;
    right: 16px;
    top: 16px;
    font-size: 18px;
    color: var(--ink-muted);
    transition: transform 0.25s ease;
    line-height: 1;
    pointer-events: none;
  }
  .grammar-mini-card.collapsed .gm-body,
  .grammar-mini-card.collapsed .gm-code {
    display: none;
  }
  .grammar-mini-card.collapsed .gm-toggle {
    transform: rotate(-90deg);
  }
  .grammar-mini-card .gm-title {
    padding-right: 28px;
  }

  /* ── Arg-Label Tooltip ── */
  .arg-label {
    cursor: help;
    position: relative;
  }
  .arg-label .arg-tooltip {
    display: none;
    position: absolute;
    bottom: calc(100% + 8px);
    left: 50%;
    transform: translateX(-50%);
    background: var(--ink);
    color: var(--bg);
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.2px;
    white-space: nowrap;
    z-index: 100;
    text-transform: none;
    font-family: var(--font-sans);
    box-shadow: 0 4px 16px rgba(0,0,0,0.18);
  }
  .arg-label .arg-tooltip::after {
    content: '';
    position: absolute;
    top: 100%;
    left: 50%;
    transform: translateX(-50%);
    border: 5px solid transparent;
    border-top-color: var(--ink);
  }
  .arg-label:hover .arg-tooltip,
  .arg-label:focus .arg-tooltip,
  .arg-label:active .arg-tooltip {
    display: block;
  }

  /* ── Section Viewed Indicator ── */
  .toc-link.viewed::before {
    width: 18px;
    opacity: 0.7;
  }
  .toc-link .toc-check {
    display: inline-block;
    width: 0;
    overflow: hidden;
    transition: width 0.3s ease, margin 0.3s ease;
    font-size: 10px;
    color: var(--color-chain);
    margin-left: 0;
  }
  .toc-link.viewed .toc-check {
    width: 14px;
    margin-left: 4px;
  }

  /* ── Task Block Copy Button ── */
  .task-block { position: relative; }
  .task-block .copy-btn { top: 20px; right: 20px; }

  /* ══════════════════════════════════════════
     READER TOOLBAR
     ══════════════════════════════════════════ */
  .reader-toolbar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 20px;
    margin-bottom: 24px;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 14px;
    box-shadow: var(--shadow-sm);
    position: sticky;
    top: 56px;
    z-index: 95;
  }
  .reader-toolbar .rt-timer {
    display: flex; align-items: center; gap: 5px;
    font-family: var(--font-mono); font-size: 13px;
    color: var(--ink-muted); font-weight: 500;
    user-select: none;
  }
  .reader-toolbar .rt-timer svg {
    width: 14px; height: 14px;
    stroke: var(--accent); stroke-width: 1.8;
    fill: none; stroke-linecap: round; stroke-linejoin: round;
  }
  .reader-toolbar .rt-timer-pause {
    width: 20px; height: 20px;
    display: flex; align-items: center; justify-content: center;
    border: none; background: transparent;
    color: var(--ink-muted);
    border-radius: 4px;
    cursor: pointer;
    font-size: 11px;
    padding: 0;
    margin-left: 2px;
    transition: all 0.15s ease;
  }
  .reader-toolbar .rt-timer-pause:hover {
    color: var(--accent);
    background: var(--accent-soft);
  }
  .reader-toolbar .rt-timer-pause svg {
    width: 12px; height: 12px;
    fill: currentColor;
    stroke: none;
  }
  .reader-toolbar .rt-divider {
    width: 1px; height: 22px;
    background: var(--border);
    flex-shrink: 0;
  }
  .reader-toolbar .rt-btn {
    display: flex; align-items: center; gap: 5px;
    padding: 6px 12px; border: 1px solid var(--border);
    border-radius: 8px; background: transparent;
    color: var(--ink-dim); font-size: 12px;
    font-family: var(--font-sans); font-weight: 500;
    cursor: pointer; transition: all 0.15s ease;
  }
  .reader-toolbar .rt-btn:hover {
    border-color: var(--accent); color: var(--accent);
    background: var(--accent-soft);
  }
  .reader-toolbar .rt-btn:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 1px;
  }
  .reader-toolbar .rt-btn.active {
    background: var(--accent-soft);
    border-color: var(--accent);
    color: var(--accent);
  }
  .reader-toolbar .rt-btn .rt-btn-icon {
    width: 14px; height: 14px;
    stroke: currentColor; stroke-width: 1.8;
    fill: none; stroke-linecap: round; stroke-linejoin: round;
  }
  .reader-toolbar .rt-spacer { flex: 1; }
  @media (max-width: 860px) {
    .reader-toolbar {
      position: static;
      padding: 8px 14px;
      margin-bottom: 14px;
    }
    .reader-toolbar .rt-btn { padding: 5px 10px; font-size: 11px; }
  }

  /* ══════════════════════════════════════════
     KEYWORDS PANEL
     ══════════════════════════════════════════ */
  .keywords-panel {
    padding: 14px 18px;
    margin-bottom: 28px;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    box-shadow: var(--shadow-sm);
  }
  .keywords-panel .kwp-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 10px;
  }
  .keywords-panel .kwp-title {
    font-size: 11px; font-weight: 600;
    letter-spacing: 0.08em; text-transform: uppercase;
    color: var(--ink-muted);
    font-family: var(--font-sans);
  }
  .keywords-panel .kwp-toggle {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 3px 10px; border: 1px solid var(--border);
    border-radius: 999px; font-size: 11px;
    color: var(--ink-muted); cursor: pointer;
    background: transparent;
    font-family: var(--font-sans); font-weight: 500;
    transition: all 0.15s ease; white-space: nowrap;
  }
  .keywords-panel .kwp-toggle:hover {
    border-color: var(--accent); color: var(--accent);
  }
  .keywords-panel .kwp-chips {
    display: flex; flex-wrap: wrap; gap: 6px;
  }
  .kt-chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 5px 11px;
    border-radius: 999px;
    font-size: 12px;
    font-family: var(--font-sans);
    font-weight: 500;
    cursor: pointer;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--ink-muted);
    transition: all 0.2s ease;
    white-space: nowrap;
    user-select: none;
  }
  .kt-chip::before {
    content: '#';
    font-weight: 700;
    opacity: 0.35;
    font-size: 11px;
  }
  .kt-chip:hover {
    border-color: var(--accent);
    color: var(--accent);
    background: var(--accent-soft);
  }
  .kt-chip.active {
    background: var(--accent-soft);
    border-color: var(--accent);
    color: var(--accent);
    font-weight: 600;
  }
  .kt-chip.kw-collapsed { display: none; }
  @media (max-width: 860px) {
    .keywords-panel {
      padding: 12px 14px;
      margin-bottom: 20px;
    }
    .keywords-panel .kwp-chips { gap: 5px; }
    .kt-chip { font-size: 11px; padding: 4px 9px; }
  }

  /* Keyword highlights */
  .kw-highlight {
    background: rgba(59,110,168,0.15);
    color: inherit;
    padding: 1px 0;
    border-radius: 2px;
    transition: background 0.2s ease;
  }
  html[data-theme="dark"] .kw-highlight {
    background: rgba(143,167,200,0.22);
  }
  .kw-highlight.focus {
    background: rgba(212,167,106,0.28);
    outline: 1px solid rgba(212,167,106,0.4);
    border-radius: 2px;
  }
  html[data-theme="dark"] .kw-highlight.focus {
    background: rgba(212,167,106,0.3);
  }

  /* ══════════════════════════════════════════
     IN-PAGE SEARCH
     ══════════════════════════════════════════ */
  .search-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.3);
    z-index: 9999;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding-top: 18vh;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.2s ease;
  }
  .search-overlay.active {
    opacity: 1;
    pointer-events: auto;
  }
  html[data-theme="dark"] .search-overlay {
    background: rgba(0,0,0,0.55);
  }
  .search-dialog {
    width: 560px;
    max-width: 90vw;
    background: var(--card-bg);
    border: 1px solid var(--border-strong);
    border-radius: 14px;
    box-shadow: var(--shadow);
    overflow: hidden;
  }
  .search-input-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
  }
  .search-input-row .si-icon {
    width: 18px; height: 18px;
    color: var(--ink-muted);
    flex-shrink: 0;
  }
  .search-input {
    flex: 1;
    border: none;
    background: transparent;
    color: var(--ink);
    font-size: 15px;
    font-family: var(--font-sans);
    outline: none;
  }
  .search-input::placeholder {
    color: var(--ink-muted);
    font-size: 14px;
  }
  .search-count {
    font-size: 12px;
    color: var(--ink-muted);
    font-family: var(--font-mono);
    white-space: nowrap;
  }
  .search-count .sc-current {
    color: var(--accent);
    font-weight: 600;
  }
  .search-nav {
    display: flex;
    gap: 2px;
  }
  .search-nav-btn {
    width: 28px; height: 28px;
    display: flex; align-items: center; justify-content: center;
    border: none; background: transparent;
    color: var(--ink-muted);
    border-radius: 6px;
    cursor: pointer;
    font-size: 16px;
    transition: all 0.15s ease;
  }
  .search-nav-btn:hover {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .search-nav-btn:disabled {
    opacity: 0.3;
    cursor: default;
  }
  .search-hint-row {
    display: flex; align-items: center; gap: 14px;
    padding: 8px 18px 14px;
    font-size: 11px;
    color: var(--ink-muted);
    font-family: var(--font-sans);
  }
  .search-hint-row kbd {
    padding: 2px 6px;
    border-radius: 4px;
    border: 1px solid var(--border);
    background: var(--bg);
    font-family: var(--font-mono);
    font-size: 10px;
  }

  /* Search highlights */
  .search-highlight {
    background: rgba(212,167,106,0.28);
    color: inherit;
    padding: 1px 0;
    border-radius: 2px;
  }
  html[data-theme="dark"] .search-highlight {
    background: rgba(212,167,106,0.32);
  }
  .search-highlight.active {
    background: rgba(212,167,106,0.5);
    outline: 2px solid var(--accent-warm);
    border-radius: 3px;
  }
  html[data-theme="dark"] .search-highlight.active {
    background: rgba(212,167,106,0.45);
  }

  /* ══════════════════════════════════════════
     KEYWORD RELATION POPOVER
     ══════════════════════════════════════════ */
  .kw-popover {
    position: absolute;
    z-index: 100;
    background: var(--card-elevated);
    border: 1px solid var(--border-strong);
    border-radius: 10px;
    padding: 14px 16px;
    box-shadow: var(--shadow);
    max-width: 320px;
    font-size: 13px;
    line-height: 1.65;
    color: var(--ink-dim);
    font-family: var(--font-sans);
    pointer-events: none;
    opacity: 0;
    transition: opacity 0.15s ease;
  }
  .kw-popover.show {
    opacity: 1;
    pointer-events: auto;
  }
  .kw-popover .kwp-term {
    font-weight: 700;
    color: var(--accent);
    margin-bottom: 6px;
    font-size: 14px;
  }
  .kw-popover .kwp-def {
    margin-bottom: 8px;
  }
  .kw-popover .kwp-related {
    font-size: 12px;
    color: var(--ink-muted);
    padding-top: 8px;
    border-top: 1px solid var(--border);
  }
  .kw-popover .kwp-related strong {
    color: var(--accent);
  }

  /* ══════════════════════════════════════════
     ARGUMENT CHAIN DEMO (issue page)
     ══════════════════════════════════════════ */
  .arg-demo-issue {
    margin-top: 56px;
    padding: 40px 32px 64px;
    border-top: 1px solid var(--border);
    max-width: 1080px;
    margin-left: auto;
    margin-right: auto;
    box-sizing: border-box;
  }
  .arg-demo-issue .demo-heading {
    margin-bottom: 24px;
  }
  .arg-demo-issue .demo-heading h2 {
    font-family: var(--font-sans);
    font-size: 20px;
    font-weight: 700;
    color: var(--ink);
    margin: 0 0 6px;
  }
  .arg-demo-issue .demo-heading .demo-sub {
    font-size: 13.5px;
    color: var(--ink-dim);
    font-family: var(--font-sans);
    line-height: 1.6;
  }
  .arg-demo-issue .demo-heading .demo-sub code {
    background: var(--accent-bg);
    color: var(--accent);
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 12px;
    font-family: var(--font-mono);
  }
  .adi-layout {
    display: grid;
    grid-template-columns: 1fr 1.2fr;
    gap: 32px;
    align-items: start;
  }
  .adi-input {
    background: var(--card-elevated);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 24px;
  }
  .adi-input h3 {
    font-family: var(--font-sans);
    font-size: 15px;
    font-weight: 700;
    margin: 0 0 4px;
    color: var(--ink);
  }
  .adi-input .input-sub {
    font-size: 12.5px;
    color: var(--ink-muted);
    margin-bottom: 16px;
    font-family: var(--font-sans);
  }
  .adi-textarea {
    width: 100%;
    min-height: 120px;
    padding: 14px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--ink);
    font-size: 14px;
    line-height: 1.7;
    resize: vertical;
    font-family: var(--font-sans);
    outline: none;
    transition: border-color 0.25s, box-shadow 0.25s;
    margin-bottom: 14px;
    box-sizing: border-box;
  }
  .adi-textarea::placeholder { color: var(--ink-muted); font-size: 13px; }
  .adi-textarea:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-subtle);
  }
  .adi-actions { display: flex; gap: 10px; align-items: center; }
  .adi-btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 10px 20px; border-radius: 8px;
    font-size: 13.5px; font-weight: 600; cursor: pointer;
    border: none; transition: all 0.25s;
    font-family: var(--font-sans); letter-spacing: 0.03em;
  }
  .adi-btn.primary {
    background: var(--accent); color: var(--on-accent);
  }
  .adi-btn.primary:hover { background: var(--accent-hover); }
  .adi-btn.secondary {
    background: transparent; color: var(--ink-dim);
    border: 1px solid var(--border);
  }
  .adi-btn.secondary:hover {
    border-color: var(--accent); color: var(--accent);
  }
  .adi-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .adi-hint {
    font-size: 11.5px;
    color: var(--ink-muted);
    margin-top: 12px;
    font-family: var(--font-sans);
    line-height: 1.5;
  }
  .adi-hint code {
    background: var(--accent-bg);
    color: var(--accent);
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 10.5px;
    font-family: var(--font-mono);
  }

  /* OUTPUT */
  .adi-output {
    display: flex; flex-direction: column; gap: 14px;
  }
  .adi-card {
    background: var(--card-elevated);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px 22px;
    box-shadow: var(--shadow-soft);
    transition: all 0.3s ease;
    position: relative;
    overflow: hidden;
  }
  .adi-card::before {
    content: '';
    position: absolute; left: 0; top: 0; bottom: 0;
    width: 3px;
    border-radius: 0 2px 2px 0;
  }
  .adi-card.card-concepts::before { background: var(--func-context); }
  .adi-card.card-chain::before { background: var(--func-chain); }
  .adi-card.card-weighing::before { background: var(--func-expressions); }
  .adi-card.card-paragraph::before { background: var(--func-sentence); }
  .adi-card .card-step {
    font-family: var(--font-mono);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    margin: 0 0 3px 6px;
  }
  .adi-card.card-concepts .card-step { color: var(--func-context); }
  .adi-card.card-chain .card-step { color: var(--func-chain); }
  .adi-card.card-weighing .card-step { color: var(--func-expressions); }
  .adi-card.card-paragraph .card-step { color: var(--func-sentence); }
  .adi-card h4 {
    font-family: var(--font-sans);
    font-size: 14px;
    font-weight: 700;
    margin: 0 0 8px 6px;
    color: var(--ink);
  }
  .adi-concepts { display: flex; flex-wrap: wrap; gap: 6px; margin-left: 6px; }
  .adi-concept-chip {
    padding: 5px 11px;
    border-radius: 5px;
    font-size: 12px;
    font-weight: 600;
    font-family: var(--font-sans);
    background: var(--accent-bg);
    color: var(--accent);
    border: 1px solid var(--accent-subtle);
  }
  .adi-chain-flow {
    display: flex; align-items: center; flex-wrap: wrap; gap: 5px;
    margin-left: 6px;
  }
  .adi-chain-node {
    padding: 5px 11px;
    border-radius: 5px;
    font-size: 12px;
    font-family: var(--font-sans);
    background: var(--accent-bg);
    color: var(--ink-dim);
    border: 1px solid var(--border);
    white-space: nowrap;
  }
  .adi-chain-arrow {
    font-size: 13px;
    color: var(--ink-muted);
    font-family: var(--font-sans);
    flex-shrink: 0;
  }
  .adi-weighing-text {
    font-size: 13.5px;
    color: var(--ink-dim);
    line-height: 1.7;
    margin-left: 6px;
    font-family: var(--font-sans);
  }
  .adi-weighing-text strong { color: var(--accent-warm); font-weight: 600; }
  .adi-paragraph {
    font-family: var(--font-serif);
    font-size: 14px;
    line-height: 1.8;
    color: var(--ink);
    background: var(--bg);
    padding: 14px 16px;
    border-radius: 6px;
    border-left: 3px solid var(--accent-subtle);
    margin-left: 6px;
  }
  .adi-paragraph em { font-style: italic; color: var(--accent); }

  /* Placeholder */
  .adi-placeholder {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    padding: 48px 24px;
    text-align: center;
    color: var(--ink-muted);
    border: 1px dashed var(--border);
    border-radius: 14px;
    background: var(--bg);
  }
  .adi-placeholder svg {
    width: 36px; height: 36px;
    stroke: var(--ink-muted); stroke-width: 1.2;
    margin-bottom: 12px; opacity: 0.35;
  }
  .adi-placeholder p {
    font-size: 13.5px; font-family: var(--font-sans);
  }
  .adi-placeholder .hint {
    font-size: 11.5px; margin-top: 5px;
    color: var(--ink-muted); opacity: 0.65;
    font-family: var(--font-sans);
  }

  /* Loading */
  .adi-loading {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    padding: 48px 24px; gap: 14px;
  }
  .adi-loading .pulse {
    width: 28px; height: 28px;
    border-radius: 50%;
    border: 3px solid var(--accent-subtle);
    border-top-color: var(--accent);
    animation: adiSpin 0.8s linear infinite;
  }
  .adi-loading p {
    font-size: 13px; color: var(--ink-dim);
    font-family: var(--font-sans);
  }
  @keyframes adiSpin { to { transform: rotate(360deg); } }

  @media (max-width: 860px) {
    .adi-layout { grid-template-columns: 1fr; }
  }
  @media (prefers-reduced-motion: reduce) {
    .adi-loading .pulse { animation: none; }
  }

  @media print {
    .arg-demo-issue { display: none !important; }
  }
"""


def _render_issue_page(md_text: str, issue_date: str = "") -> str:
    """Convert ArgueLab v2 briefing markdown into a full self-contained HTML page.

    This renders the 6-pane briefing as a beautiful standalone web page with:
    - Light paper reading mode (default) with dark mode toggle
    - Functional color system per module
    - Card-based content chunking
    - Sticky TOC navigation
    - Proper typography and visual rhythm
    - Theme persistence via localStorage
    """
    # Strip YAML frontmatter
    if md_text.startswith("---"):
        end_fm = md_text.index("---", 3)
        md_text = md_text[end_fm + 3:].strip()

    lines = md_text.strip().split("\n")

    # Parse metadata from H1
    title = "ArgueLab — Training Briefing"
    date_str = issue_date or datetime.now().strftime("%B %d, %Y")
    topic_line = ""
    training_focus = ""
    issue_number = ""

    for line in lines[:10]:
        s = line.strip()
        m = re.match(r'^#\s+ArgueLab.*\|\s*(.+)$', s)
        if m:
            date_str = m.group(1).strip()
        m = re.match(r'>\s*\*\*今日议题：\*\*\s*(.+)', s)
        if m:
            topic_line = m.group(1).strip()
        m = re.match(r'>\s*\*\*训练重点：\*\*\s*(.+)', s)
        if m:
            training_focus = m.group(1).strip()

    # Build issue number slug
    if issue_date:
        try:
            dt = datetime.strptime(issue_date, "%Y-%m-%d")
            issue_number = dt.strftime("#%Y%m%d")
        except ValueError:
            issue_number = issue_date

    # Parse sections
    sections = []
    current_section = None
    current_lines = []
    in_code_block = False
    code_lines = []

    i = 0
    # Skip H1 header and metadata lines until first ##
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("## "):
            break
        i += 1

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Code blocks
        if stripped.startswith("```"):
            if in_code_block:
                current_lines.append(("code", "\n".join(code_lines)))
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

        # H2 section headers
        if re.match(r'^##\s+\d+\.', stripped):
            if current_section and current_lines:
                sections.append((current_section, current_lines))
            current_section = re.sub(r'^##\s+\d+\.\s*', '', stripped)
            current_lines = []
            i += 1
            continue

        # H2 fallback
        if stripped.startswith("## ") and not stripped.startswith("### "):
            if current_section and current_lines:
                sections.append((current_section, current_lines))
            current_section = stripped[3:].strip()
            current_lines = []
            i += 1
            continue

        current_lines.append(("text", stripped))
        i += 1

    if current_section and current_lines:
        sections.append((current_section, current_lines))

    # ── Determine section module types ──
    # Map section index to module type: 0=context, 1=passage, 2=expression, 3=sentence, 4=chain, 5=output
    MODULE_MAP = ["context", "passage", "expression", "sentence", "chain", "output"]
    MODULE_NAMES = {
        "context": "Context",
        "passage": "Core Passage",
        "expression": "Expression Transfer",
        "sentence": "Sentence Anatomy",
        "chain": "Argument Chain",
        "output": "Output Tasks",
    }
    TOC_CLASSES = {
        "context": "toc-context", "passage": "toc-passage",
        "expression": "toc-expression", "sentence": "toc-sentence",
        "chain": "toc-chain", "output": "toc-output",
    }

    # ── Extract keywords for toolbar ──
    # Collect expression phrases and bold terms for interactive keyword features
    keywords = []
    seen_kw = set()
    for idx, (section_title, section_items) in enumerate(sections):
        if idx >= len(MODULE_MAP):
            break
        mod = MODULE_MAP[idx]
        if mod == "expression":
            # Extract expression phrases from backtick-enclosed text (e.g., `the tyranny of merit`)
            for item_type, item_text in section_items:
                # Match `phrase` — the actual expression terms
                for m in re.finditer(r'`([^`]{3,80})`', item_text):
                    kw = m.group(1).strip()
                    # Filter: must contain letters, not a label, and be a concise phrase (not a full sentence)
                    if kw and re.search(r'[a-zA-Z]', kw) and kw not in seen_kw and '：' not in kw and ':' not in kw:
                        # Skip full sentences (too long for keyword chips)
                        if len(kw) <= 45 and kw.count(' ') <= 5:
                            seen_kw.add(kw)
                            keywords.append(kw)
                # Also match **phrase** bold patterns (for rich-format briefings)
                for m in re.finditer(r'\*\*([^*]{3,60})\*\*', item_text):
                    kw = m.group(1).strip()
                    if kw and not re.match(r'^(功能|语域|语义|标签|适用|常见|结构|例|注意|英文表达|中文释义|外刊例句|仿写参考|常见搭配|适用场景|论证功能|修辞功能|语法结构|仿写模板)', kw) and '：' not in kw and ':' not in kw and kw not in seen_kw:
                        if len(kw) <= 45 and kw.count(' ') <= 5:
                            seen_kw.add(kw)
                            keywords.append(kw)
        elif mod == "context":
            # Extract bold terms from context (framing terms, key concepts)
            for item_type, item_text in section_items:
                # Look for terms like **tyranny of merit**
                for m in re.finditer(r'\*\*([^*]{2,40})\*\*', item_text):
                    kw = m.group(1).strip()
                    if kw and len(kw) > 3 and not kw.startswith('议题') and not kw.startswith('背景') and not kw.startswith('争议') and not kw.startswith('为什么') and not kw.startswith('Framing') and kw not in seen_kw:
                        seen_kw.add(kw)
                        keywords.append(kw)

    # Limit to ~12 keywords for toolbar
    keywords = keywords[:12]

    # Generate keywords panel HTML (first 6 visible, rest collapsible)
    keywords_panel_html = ""
    if keywords:
        chips = []
        for i, kw in enumerate(keywords):
            safe_kw = _escape_html(kw)
            collapsed = ' kw-collapsed' if i >= 6 else ''
            dc_attr = ' data-collapsed="true"' if i >= 6 else ''
            chips.append(f'<span class="kt-chip{collapsed}" data-kw="{safe_kw}"{dc_attr} role="button" tabindex="0">{safe_kw}</span>')
        show_all_btn = ''
        if len(keywords) > 6:
            show_all_btn = '<button class="kwp-toggle" id="kw-show-all" aria-expanded="false">Show all ({n})</button>'.format(n=len(keywords))
        keywords_panel_html = (
            '<div class="keywords-panel" id="keywords-panel">'
            '<div class="kwp-header">'
            '<span class="kwp-title">Keywords</span>'
            + show_all_btn +
            '</div>'
            '<div class="kwp-chips">'
            + "".join(chips) +
            '</div>'
            '</div>'
        )

    # TOC (desktop) — rendered as <aside> before main content
    toc_items = []
    for idx, (section_title, _) in enumerate(sections):
        if idx >= len(MODULE_MAP):
            break  # Only show the 6 main modules in TOC
        mod = MODULE_MAP[idx]
        toc_items.append(
            '<li><a href="#section-{idx}" class="toc-link {toc_cls}" data-section="{idx}">'
            '<span class="toc-num">{num:02d}</span> {name}</a></li>'.format(
                idx=idx, toc_cls=TOC_CLASSES.get(mod, ""),
                num=idx + 1, name=MODULE_NAMES.get(mod, section_title)
            )
        )

    toc_desktop = (
        '<aside class="issue-toc"><div class="toc-header">'
        '<div class="toc-label">In This Issue</div>'
        '<button class="toc-toggle" id="toc-toggle-btn" aria-label="Collapse sidebar" title="Collapse sidebar">'
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>'
        '</button></div>'
        '<ul class="toc-list">{}</ul></aside>'.format("".join(toc_items))
    )

    # Mobile TOC (inside main content, sticky at top on mobile)
    mobile_chips = []
    for idx, (section_title, _) in enumerate(sections):
        if idx >= len(MODULE_MAP):
            break
        mod = MODULE_MAP[idx]
        mobile_chips.append(
            '<a href="#section-{idx}" class="toc-chip {toc_cls}" data-section="{idx}">{name}</a>'.format(
                idx=idx, toc_cls=TOC_CLASSES.get(mod, ""),
                name=MODULE_NAMES.get(mod, section_title)
            )
        )
    mobile_toc = '<nav class="toc-mobile">{}</nav>'.format("".join(mobile_chips))

    # ── Render HTML ──
    html_parts = []

    # Mobile TOC (sticky at top of page, hidden on desktop via CSS)
    html_parts.insert(0, mobile_toc)

    # 1. Issue hero — visual center of the first screen
    html_parts.append(f'''<div class="issue-hero">
  <div class="issue-kicker">{_escape_html(issue_number)}</div>
  <h1>{_escape_html(title)}</h1>
  <p class="issue-title">{_escape_html(topic_line)}</p>
  <p class="issue-meta">{_escape_html(date_str)} &middot; {_escape_html(training_focus)}</p>
</div>''')

    # 2. Reader toolbar (between hero and keywords: timer, keywords toggle)
    kw_toggle_btn = ''
    if keywords:
        kw_toggle_btn = '<button class="rt-btn" id="btn-toggle-kw" title="Toggle keywords panel" style="margin-left:auto">Keywords</button>'
    reader_toolbar_html = (
        '<div class="reader-toolbar" id="reader-toolbar">'
        '<span class="rt-timer" id="reader-timer" title="Time reading this issue">'
        '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>'
        '<span id="rt-timer-text">00:00</span>'
        '<button class="rt-timer-pause" id="btn-timer-pause" title="Pause / Resume timer" aria-label="Pause timer">'
        '<svg id="timer-pause-icon" viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>'
        '<svg id="timer-play-icon" viewBox="0 0 24 24" style="display:none"><polygon points="5,3 19,12 5,21"/></svg>'
        '</button>'
        '</span>'
        '<span class="rt-divider"></span>'
        + kw_toggle_btn +
        '</div>'
    )
    html_parts.append(reader_toolbar_html)

    # 3. Keywords panel (collapsible, default shows first 6)
    if keywords_panel_html:
        html_parts.append(keywords_panel_html)

    # Default source badges per module type
    DEFAULT_BADGES = {
        "context": [
            ("source", "SOURCE-BASED", "Based on cited public sources. Adapted for training."),
            ("ai", "AI-ASSISTED SUMMARY", "Background based on public reporting and structured by ArgueLab."),
        ],
        "passage": [
            ("training", "TRAINING PASSAGE", "Written in an editorial style for argument analysis. Not a verbatim excerpt."),
            ("source", "SOURCE-BASED", "Based on cited public sources, adapted for training."),
        ],
        "expression": [
            ("ai", "AI-ASSISTED ANALYSIS", "Expression functions and examples generated for transfer practice."),
        ],
        "sentence": [
            ("ai", "AI-ASSISTED ANALYSIS", "Sentence structure and reusable templates generated for learning."),
        ],
        "chain": [
            ("ai", "AI-ASSISTED ARGUMENT TRAINING", "Chinese-to-English argument chain generated for practice."),
        ],
        "output": [
            ("practice", "USER PRACTICE", "Use this section to write, speak, and revise your own argument."),
        ],
    }

    # Attempt to parse sources from markdown (look for Section 7 or "## 7" or "Sources")
    sources = []
    in_sources = False
    for line in lines:
        s = line.strip()
        if re.match(r'^##\s+7\.', s) or re.match(r'^##\s+Sources', s, re.IGNORECASE):
            in_sources = True
            continue
        if in_sources:
            if s.startswith("## "):
                in_sources = False
            elif s and not s.startswith("#"):
                sources.append(s)

    # Render each section
    for idx, (section_title, section_items) in enumerate(sections):
        if idx >= len(MODULE_MAP):
            break  # Only render the 6 main modules
        mod = MODULE_MAP[idx]
        section_num = idx + 1

        html_parts.append(
            '<section id="section-{idx}" class="issue-section section-{mod}">'.format(
                idx=idx, mod=mod
            )
        )

        # Section heading with badge
        html_parts.append(
            '<div class="section-heading">'
            '<span class="section-badge">{num:02d}</span>'
            '<div><h2>{title}</h2></div>'
            '</div>'.format(num=section_num, title=_escape_html(section_title))
        )

        # Source badges (default per module type)
        badges = DEFAULT_BADGES.get(mod, [])
        if badges:
            badge_html = []
            first_note = ""
            for badge_type, badge_text, badge_note in badges:
                badge_html.append(
                    '<span class="source-badge {cls}">{text}</span>'.format(
                        cls=badge_type, text=_escape_html(badge_text)
                    )
                )
                if not first_note and badge_note:
                    first_note = badge_note
            html_parts.append(
                '<div class="source-badge-row">{}</div>'.format("".join(badge_html))
            )
            if first_note:
                html_parts.append(
                    '<p class="source-note">{}</p>'.format(_escape_html(first_note))
                )

        # For all sections (context, passage, expression, sentence, chain, output),
        # combine all text into one block so specialized renderers can detect the full structure.
        if mod in ("passage", "expression", "sentence", "chain", "output", "context"):
            all_text = []
            for item_type, item_text in section_items:
                if item_type == "code":
                    all_text.append(item_text)
                else:
                    all_text.append(item_text)
            combined = "\n".join(all_text)
            html_parts.append(_render_paragraph(combined, mod))
        else:
            # Paragraph-by-paragraph rendering for context
            para = []
            for item_type, item_text in section_items:
                if item_type == "code":
                    if para:
                        html_parts.append(_render_paragraph("\n".join(para), mod))
                        para = []
                    html_parts.append(
                        '<pre class="template-box">{}</pre>'.format(_escape_html(item_text))
                    )
                    continue

                if item_text == "":
                    if para:
                        html_parts.append(_render_paragraph("\n".join(para), mod))
                        para = []
                else:
                    para.append(item_text)

            if para:
                html_parts.append(_render_paragraph("\n".join(para), mod))

        html_parts.append('</section>')

    # Source list (if sources were parsed)
    if sources:
        src_items = ""
        for s in sources:
            s = s.strip()
            if not s or s.startswith("*") and s.endswith("*"):
                continue  # skip italic metadata lines
            if s.startswith("```") or s == "---":
                continue
            src_items += "<li>{}</li>".format(_markdown_inline_to_html(s))
        if src_items:
            html_parts.append(
                '<div class="source-list">'
                '<div class="source-list-title">Sources Used in This Issue</div>'
                '<ul>{}</ul>'
                '</div>'.format(src_items)
            )

    # Footer
    html_parts.append(f'''<div class="page-footer">
  <p class="brand">Argue<span>Lab</span></p>
  <p class="slogan">Read like a scholar. Argue like a native.</p>
</div>''')

    body = "\n".join(html_parts)

  # JS for TOC scroll spy
    toc_js = """
<script>
(function() {
  var tocItems = document.querySelectorAll('.toc-link, .toc-chip');
  var sections = document.querySelectorAll('.issue-section');
  var toc = document.querySelector('.issue-toc');
  var shell = document.querySelector('.issue-shell');
  var toggleBtn = document.getElementById('toc-toggle-btn');
  var STORAGE_KEY = 'arguelab-toc-collapsed';

  // ── Collapse / Expand ──
  function setCollapsed(state) {
    if (state) {
      toc.classList.add('collapsed');
      shell.classList.add('toc-collapsed');
      toggleBtn.setAttribute('aria-label', 'Expand sidebar');
      toggleBtn.setAttribute('title', 'Expand sidebar');
    } else {
      toc.classList.remove('collapsed');
      shell.classList.remove('toc-collapsed');
      toggleBtn.setAttribute('aria-label', 'Collapse sidebar');
      toggleBtn.setAttribute('title', 'Collapse sidebar');
    }
  }

  if (toggleBtn) {
    // Restore persisted state
    var savedCollapsed = localStorage.getItem(STORAGE_KEY);
    if (savedCollapsed === 'true') {
      setCollapsed(true);
    }

    toggleBtn.addEventListener('click', function() {
      var isCollapsed = toc.classList.contains('collapsed');
      setCollapsed(!isCollapsed);
      localStorage.setItem(STORAGE_KEY, isCollapsed ? 'false' : 'true');
    });
  }

  if (!tocItems.length || !sections.length) return;

  function updateActive() {
    var scrollY = window.scrollY + 120;
    var current = -1;
    sections.forEach(function(sec, i) {
      if (sec.offsetTop <= scrollY) current = i;
    });
    tocItems.forEach(function(item) {
      var sectionIdx = parseInt(item.getAttribute('data-section'));
      if (sectionIdx === current) {
        item.classList.add('active');
      } else {
        item.classList.remove('active');
      }
    });
  }

  window.addEventListener('scroll', updateActive, { passive: true });
  updateActive();

  // Smooth scroll for TOC links
  tocItems.forEach(function(item) {
    item.addEventListener('click', function(e) {
      e.preventDefault();
      var target = document.querySelector(item.getAttribute('href'));
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  });
})();
</script>
"""

    # Theme toggle JS
    theme_js = """
<script>
(function() {
  var STORAGE_KEY = 'arguelab-theme';
  var toggleBtn = document.getElementById('theme-toggle-btn');
  var iconSun = document.getElementById('icon-sun');
  var iconMoon = document.getElementById('icon-moon');
  var toggleLabel = document.getElementById('toggle-label');
  if (!toggleBtn) return;

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem(STORAGE_KEY, theme);
    if (theme === 'dark') {
      if (iconSun) iconSun.style.display = 'none';
      if (iconMoon) iconMoon.style.display = 'block';
      toggleLabel.textContent = 'Dark Mode';
    } else {
      if (iconSun) iconSun.style.display = 'block';
      if (iconMoon) iconMoon.style.display = 'none';
      toggleLabel.textContent = 'Reading Mode';
    }
  }

  // Default: light. Check localStorage first, then system preference.
  var savedTheme = localStorage.getItem(STORAGE_KEY);
  if (savedTheme) {
    applyTheme(savedTheme);
  }

  toggleBtn.addEventListener('click', function() {
    var current = document.documentElement.getAttribute('data-theme');
    var next = (current === 'dark') ? 'light' : 'dark';
    applyTheme(next);
  });
})();
</script>
"""
    # ── Interaction JS ──
    interaction_js = """
<script>
(function() {
  var ISSUE_DATE = '""" + issue_date + """';

  // ══════════════════════════════════════════
  // 1. TOAST SYSTEM
  // ══════════════════════════════════════════
  var toastTimer = null;
  function showToast(msg) {
    var toast = document.getElementById('interaction-toast');
    if (!toast) {
      toast = document.createElement('div');
      toast.id = 'interaction-toast';
      toast.className = 'interaction-toast';
      document.body.appendChild(toast);
    }
    toast.textContent = msg;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function() {
      toast.classList.remove('show');
    }, 2000);
  }

  // ══════════════════════════════════════════
  // 2. COPY TO CLIPBOARD
  // ══════════════════════════════════════════
  function makeCopyBtn(targetSelector, extractFn) {
    return function() {
      var btn = document.createElement('button');
      btn.className = 'copy-btn';
      btn.innerHTML = '&#x2398;';
      btn.setAttribute('aria-label', 'Copy to clipboard');
      btn.addEventListener('click', function(e) {
        e.stopPropagation();
        var text = extractFn(this.parentElement);
        copyText(text, this);
      });
      return btn;
    };
  }

  function copyText(text, btn) {
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function() {
        onCopySuccess(btn);
      }).catch(function() {
        fallbackCopy(text, btn);
      });
    } else {
      fallbackCopy(text, btn);
    }
  }

  function fallbackCopy(text, btn) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); onCopySuccess(btn); } catch(e) {}
    document.body.removeChild(ta);
  }

  function onCopySuccess(btn) {
    btn.classList.add('copied');
    showToast('Copied \\u2713');
    setTimeout(function() {
      btn.classList.remove('copied');
    }, 1500);
  }

  // Inject copy buttons into cards
  function injectCopyButtons() {
    // Expression phrase + example
    document.querySelectorAll('.expr-card').forEach(function(card) {
      card.classList.add('card-has-copy');
      // Copy phrase
      var phraseEl = card.querySelector('.expr-phrase');
      if (phraseEl) {
        var btn = document.createElement('button');
        btn.className = 'copy-btn';
        btn.innerHTML = '&#x2398;';
        btn.setAttribute('aria-label', 'Copy expression');
        btn.addEventListener('click', function(e) {
          e.stopPropagation();
          copyText(phraseEl.textContent.trim(), btn);
        });
        card.appendChild(btn);
      }
    });

    // Target sentence
    document.querySelectorAll('.target-sentence-card').forEach(function(card) {
      card.classList.add('card-has-copy');
      var textEl = card.querySelector('.ts-text');
      if (textEl) {
        var btn = document.createElement('button');
        btn.className = 'copy-btn';
        btn.innerHTML = '&#x2398;';
        btn.setAttribute('aria-label', 'Copy sentence');
        btn.addEventListener('click', function(e) {
          e.stopPropagation();
          copyText(textEl.textContent.trim(), btn);
        });
        card.appendChild(btn);
      }
    });

    // Template card
    document.querySelectorAll('.template-card').forEach(function(card) {
      card.classList.add('card-has-copy');
      var codeEl = card.querySelector('.tpl-code');
      if (codeEl) {
        var btn = document.createElement('button');
        btn.className = 'copy-btn';
        btn.innerHTML = '&#x2398;';
        btn.setAttribute('aria-label', 'Copy template');
        btn.addEventListener('click', function(e) {
          e.stopPropagation();
          copyText(codeEl.textContent.trim(), btn);
        });
        card.appendChild(btn);
      }
    });

    // Task prompts
    document.querySelectorAll('.task-prompt').forEach(function(el) {
      el.parentElement.classList.add('card-has-copy');
      if (!el.parentElement.classList.contains('task-block')) {
        el.parentElement.style.position = 'relative';
      }
      var btn = document.createElement('button');
      btn.className = 'copy-btn';
      btn.innerHTML = '&#x2398;';
      btn.setAttribute('aria-label', 'Copy task prompt');
      btn.addEventListener('click', function(e) {
        e.stopPropagation();
        copyText(el.textContent.trim(), btn);
      });
      el.parentElement.appendChild(btn);
    });

    // Sample paragraph card
    document.querySelectorAll('.sample-paragraph-card').forEach(function(card) {
      card.classList.add('card-has-copy');
      var textEl = card.querySelector('.sp-text');
      if (textEl) {
        var btn = document.createElement('button');
        btn.className = 'copy-btn';
        btn.innerHTML = '&#x2398;';
        btn.setAttribute('aria-label', 'Copy paragraph');
        btn.addEventListener('click', function(e) {
          e.stopPropagation();
          copyText(textEl.textContent.trim(), btn);
        });
        card.appendChild(btn);
      }
    });
  }

  // ══════════════════════════════════════════
  // 3. SELF-CHECK CHECKLIST TOGGLE
  // ══════════════════════════════════════════
  function initChecklist() {
    var storageKey = 'arguelab-checks-' + ISSUE_DATE;
    var checked = {};
    try {
      checked = JSON.parse(localStorage.getItem(storageKey)) || {};
    } catch(e) {}

    document.querySelectorAll('.check-card .checklist li').forEach(function(li, idx) {
      var card = li.closest('.check-card');
      var cardIdx = Array.from(card.parentElement.querySelectorAll('.check-card')).indexOf(card);
      var key = cardIdx + '-' + idx;

      // Restore state
      if (checked[key]) {
        li.classList.add('checked');
      }

      li.addEventListener('click', function() {
        li.classList.toggle('checked');
        checked[key] = li.classList.contains('checked');
        try {
          localStorage.setItem(storageKey, JSON.stringify(checked));
        } catch(e) {}
      });
    });
  }

  // ══════════════════════════════════════════
  // 4. GRAMMAR CARD EXPAND/COLLAPSE
  // ══════════════════════════════════════════
  function initGrammarToggle() {
    var storageKey = 'arguelab-grammar-' + ISSUE_DATE;
    var collapsed = {};
    try {
      collapsed = JSON.parse(localStorage.getItem(storageKey)) || {};
    } catch(e) {}

    document.querySelectorAll('.grammar-mini-card').forEach(function(card, idx) {
      var toggle = document.createElement('span');
      toggle.className = 'gm-toggle';
      toggle.innerHTML = '&#x25BC;';
      card.appendChild(toggle);

      // Restore state
      if (collapsed[idx] === false) {
        // default: expanded
      } else {
        // default: collapsed
        card.classList.add('collapsed');
      }

      card.addEventListener('click', function() {
        card.classList.toggle('collapsed');
        collapsed[idx] = !card.classList.contains('collapsed');
        try {
          localStorage.setItem(storageKey, JSON.stringify(collapsed));
        } catch(e) {}
      });
    });
  }

  // ══════════════════════════════════════════
  // 5. SECTION VIEWED TRACKING
  // ══════════════════════════════════════════
  function initSectionTracking() {
    var storageKey = 'arguelab-viewed-' + ISSUE_DATE;
    var viewed = {};
    try {
      viewed = JSON.parse(localStorage.getItem(storageKey)) || {};
    } catch(e) {}

    var sections = document.querySelectorAll('.issue-section');

    // Apply initial viewed state
    sections.forEach(function(sec, i) {
      if (viewed[i]) {
        markViewed(sec, i);
      }
    });

    // Track on scroll
    var observer = new IntersectionObserver(function(entries) {
      entries.forEach(function(entry) {
        if (entry.isIntersecting) {
          var idx = Array.from(sections).indexOf(entry.target);
          if (idx >= 0) {
            viewed[idx] = true;
            markViewed(entry.target, idx);
            try {
              localStorage.setItem(storageKey, JSON.stringify(viewed));
            } catch(e) {}
          }
        }
      });
    }, { threshold: 0.3 });

    sections.forEach(function(sec) {
      observer.observe(sec);
    });
  }

  function markViewed(section, idx) {
    section.classList.add('section-viewed');
    var tocLink = document.querySelector('.toc-link[data-section="' + idx + '"]');
    if (tocLink && !tocLink.classList.contains('viewed')) {
      tocLink.classList.add('viewed');
      var check = tocLink.querySelector('.toc-check');
      if (!check) {
        check = document.createElement('span');
        check.className = 'toc-check';
        check.innerHTML = '\\2713';
        tocLink.appendChild(check);
      }
    }
  }

  // ══════════════════════════════════════════
  // 6. ARG LABEL TOOLTIPS
  // ══════════════════════════════════════════
  function initArgTooltips() {
    var labels = {
      'Thesis': 'Main argument / central claim',
      'Premise': 'Underlying reason / assumption',
      'Evidence': 'Supporting data / examples',
      'Counter-arg': 'Opposing viewpoint addressed',
      'Conclusion': 'Summary / final position'
    };

    document.querySelectorAll('.arg-label').forEach(function(label) {
      var text = label.textContent.trim();
      // Extract label key
      for (var key in labels) {
        if (text.indexOf(key) >= 0 || key.indexOf(text) >= 0) {
          var tt = document.createElement('span');
          tt.className = 'arg-tooltip';
          tt.textContent = labels[key];
          label.appendChild(tt);
          break;
        }
      }
    });
  }

  // ══════════════════════════════════════════
  // 7. READING TIMER
  // ══════════════════════════════════════════
  var readingSeconds = 0;
  var readingTimerRaf = null;   // requestAnimationFrame handle
  var readingStorageKey = 'arguelab-rt-' + ISSUE_DATE;

  function initReadingTimer() {
    var timerEl = document.getElementById('rt-timer-text');
    if (!timerEl) return;

    // Restore accumulated time
    try {
      var saved = parseInt(localStorage.getItem(readingStorageKey));
      if (saved > 0) readingSeconds = saved;
    } catch(e) {}

    function updateDisplay() {
      var m = Math.floor(readingSeconds / 60);
      var s = readingSeconds % 60;
      timerEl.textContent = (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
    }
    updateDisplay();

    // Timer state
    var timerPaused = false;
    var lastTick = Date.now();

    // Core tick: Date.now()-based accurate elapsed counting.
    // Always runs — even if the page is hidden (iframe preview, background tab).
    // Hidden-time skipping is handled by visibilitychange, not here.
    function timerTick() {
      if (timerPaused) return;

      var now = Date.now();
      var elapsed = Math.floor((now - lastTick) / 1000);
      if (elapsed > 0) {
        readingSeconds += elapsed;
        lastTick = now;
        updateDisplay();
        try {
          localStorage.setItem(readingStorageKey, readingSeconds);
        } catch(e) {}
      }
    }

    // Primary: setInterval at 1s (works in all contexts — iframe, hidden, background)
    // Secondary: RAF for smoother display updates while page is visible
    function startRafLoop() {
      if (readingTimerRaf) cancelAnimationFrame(readingTimerRaf);
      function loop() {
        if (timerPaused) { readingTimerRaf = null; return; }
        // RAF only runs when visible — browsers throttle it when hidden.
        // setInterval (below) handles the fallback counting.
        if (!document.hidden) {
          timerTick();
        }
        readingTimerRaf = requestAnimationFrame(loop);
      }
      readingTimerRaf = requestAnimationFrame(loop);
    }

    // Robust fallback: setInterval always runs, even in iframes / background tabs.
    // Uses Date.now() diff so it never double-counts alongside RAF.
    var timerInterval = setInterval(timerTick, 1000);

    // ── Bootstrap ──
    startRafLoop();

    // ── Visibility change: skip hidden time ──
    document.addEventListener('visibilitychange', function() {
      if (document.hidden) {
        // Going hidden: reset lastTick so hidden seconds aren't counted
        lastTick = Date.now();
      } else {
        // Coming back: reset lastTick to start fresh
        lastTick = Date.now();
      }
    });

    // ── Pause / Resume button ──
    var pauseBtn = document.getElementById('btn-timer-pause');
    var pauseIcon = document.getElementById('timer-pause-icon');
    var playIcon = document.getElementById('timer-play-icon');
    if (pauseBtn) {
      pauseBtn.addEventListener('click', function() {
        timerPaused = !timerPaused;
        lastTick = Date.now();
        if (timerPaused) {
          pauseIcon.style.display = 'none';
          playIcon.style.display = 'block';
          pauseBtn.setAttribute('title', 'Resume timer');
          pauseBtn.setAttribute('aria-label', 'Resume timer');
        } else {
          pauseIcon.style.display = 'block';
          playIcon.style.display = 'none';
          pauseBtn.setAttribute('title', 'Pause timer');
          pauseBtn.setAttribute('aria-label', 'Pause timer');
        }
      });
    }
  }

  // ══════════════════════════════════════════
  // 8. KEYWORD HIGHLIGHTING
  // ══════════════════════════════════════════
  var currentKeyword = null;
  var kwHighlights = [];

  function clearKeywordHighlights() {
    kwHighlights.forEach(function(el) {
      var parent = el.parentNode;
      parent.replaceChild(document.createTextNode(el.textContent), el);
      if (parent.normalize) parent.normalize();
    });
    kwHighlights = [];
    document.querySelectorAll('.kt-chip').forEach(function(c) { c.classList.remove('active'); });
    currentKeyword = null;
  }

  function highlightKeyword(kw) {
    clearKeywordHighlights();
    if (!kw) return;

    currentKeyword = kw;
    var chips = document.querySelectorAll('.kt-chip');
    chips.forEach(function(c) {
      if (c.getAttribute('data-kw') === kw) c.classList.add('active');
    });

    // Find and highlight all text node occurrences
    var main = document.querySelector('.issue-main');
    if (!main) return;
    var regex = new RegExp('(' + escapeRegex(kw) + ')', 'gi');
    highlightTextNodes(main, regex, 'kw-highlight', kwHighlights);

    // Scroll first highlight into view
    if (kwHighlights.length > 0) {
      kwHighlights[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }

  function escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
  }

  function highlightTextNodes(node, regex, className, resultArray) {
    if (node.nodeType === 3) { // Text node
      var text = node.textContent;
      var match;
      var lastIndex = 0;
      var fragments = [];
      regex.lastIndex = 0;
      while ((match = regex.exec(text)) !== null) {
        if (match.index > lastIndex) {
          fragments.push(document.createTextNode(text.slice(lastIndex, match.index)));
        }
        var mark = document.createElement('mark');
        mark.className = className;
        mark.textContent = match[0];
        fragments.push(mark);
        resultArray.push(mark);
        lastIndex = regex.lastIndex;
        if (match[0].length === 0) regex.lastIndex++;
      }
      if (lastIndex < text.length) {
        fragments.push(document.createTextNode(text.slice(lastIndex)));
      }
      if (fragments.length > 0) {
        var parent = node.parentNode;
        fragments.forEach(function(f) { parent.insertBefore(f, node); });
        parent.removeChild(node);
      }
    } else if (node.nodeType === 1) { // Element node
      // Skip script, style, and already highlighted elements
      var tag = node.tagName.toLowerCase();
      if (tag === 'script' || tag === 'style' || tag === 'mark' || tag === 'textarea' || tag === 'input') return;
      // Process children (use a copy since we may modify)
      Array.from(node.childNodes).forEach(function(child) {
        highlightTextNodes(child, regex, className, resultArray);
      });
    }
  }

  function initKeywordToolbar() {
    var panel = document.getElementById('keywords-panel');
    if (!panel) return;

    panel.addEventListener('click', function(e) {
      var chip = e.target.closest('.kt-chip');
      if (!chip) return;
      var kw = chip.getAttribute('data-kw');
      if (currentKeyword === kw) {
        clearKeywordHighlights();
      } else {
        highlightKeyword(kw);
      }
    });

    // Keyboard accessibility
    panel.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' || e.key === ' ') {
        var chip = e.target.closest('.kt-chip');
        if (chip) {
          e.preventDefault();
          chip.click();
        }
      }
    });
  }

  // ══════════════════════════════════════════
  // 9. IN-PAGE SEARCH (top bar primary + overlay fallback)
  // ══════════════════════════════════════════
  var searchMatches = [];
  var searchMatchIdx = -1;

  function initInPageSearch() {
    // ── Top bar search elements ──
    var tbInput = document.getElementById('top-search-input');
    var tbCount = document.getElementById('top-search-count');
    var tbPrev = document.getElementById('top-search-prev');
    var tbNext = document.getElementById('top-search-next');
    var tbClear = document.getElementById('top-search-clear');

    // ── Overlay fallback elements ──
    var overlay = document.getElementById('search-overlay');
    var ovInput = document.getElementById('search-input');
    var ovCount = document.getElementById('search-count');
    var ovPrev = document.getElementById('search-prev');
    var ovNext = document.getElementById('search-next');
    var ovClose = document.getElementById('search-close');

    // ── Shared state ──
    var currentQuery = '';

    function clearSearchHighlights() {
      searchMatches.forEach(function(mark) {
        var parent = mark.parentNode;
        if (parent) {
          parent.replaceChild(document.createTextNode(mark.textContent), mark);
          if (parent.normalize) parent.normalize();
        }
      });
      searchMatches = [];
      searchMatchIdx = -1;
    }

    function doSearch(query) {
      clearSearchHighlights();
      currentQuery = query;
      if (!query) {
        updateAllNav();
        return;
      }
      var main = document.querySelector('.issue-main');
      if (!main) return;
      var regex = new RegExp('(' + escapeRegex(query) + ')', 'gi');
      var results = [];
      findTextMatches(main, regex, results);

      var marks = [];
      results.forEach(function(node) {
        var mark = document.createElement('mark');
        mark.className = 'search-highlight';
        mark.textContent = node.textContent;
        var parent = node.parentNode;
        parent.insertBefore(mark, node);
        parent.removeChild(node);
        marks.push(mark);
      });
      searchMatches = marks;

      if (searchMatches.length > 0) {
        searchMatchIdx = 0;
        searchMatches[0].classList.add('active');
        searchMatches[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      updateAllNav();
    }

    function findTextMatches(node, regex, results) {
      if (node.nodeType === 3) {
        regex.lastIndex = 0;
        if (regex.test(node.textContent)) {
          results.push(node);
        }
      } else if (node.nodeType === 1) {
        var tag = node.tagName.toLowerCase();
        if (tag === 'script' || tag === 'style' || tag === 'mark' || tag === 'textarea' || tag === 'input') return;
        Array.from(node.childNodes).forEach(function(child) {
          findTextMatches(child, regex, results);
        });
      }
    }

    function navigateSearch(dir) {
      if (searchMatches.length === 0) return;
      if (searchMatchIdx >= 0 && searchMatchIdx < searchMatches.length) {
        searchMatches[searchMatchIdx].classList.remove('active');
      }
      searchMatchIdx += dir;
      if (searchMatchIdx < 0) searchMatchIdx = searchMatches.length - 1;
      if (searchMatchIdx >= searchMatches.length) searchMatchIdx = 0;
      searchMatches[searchMatchIdx].classList.add('active');
      searchMatches[searchMatchIdx].scrollIntoView({ behavior: 'smooth', block: 'center' });
      updateAllNav();
    }

    function updateAllNav() {
      updateTopBarNav();
      updateOverlayNav();
    }

    function updateTopBarNav() {
      if (!tbCount || !tbPrev || !tbNext || !tbClear) return;
      if (searchMatches.length > 0) {
        tbCount.textContent = (searchMatchIdx + 1) + '/' + searchMatches.length;
        tbCount.classList.add('visible');
        tbPrev.classList.add('visible');
        tbPrev.disabled = false;
        tbNext.classList.add('visible');
        tbNext.disabled = false;
        tbClear.classList.add('visible');
      } else if (currentQuery) {
        tbCount.textContent = '0/0';
        tbCount.classList.add('visible');
        tbPrev.classList.add('visible'); tbPrev.disabled = true;
        tbNext.classList.add('visible'); tbNext.disabled = true;
        tbClear.classList.add('visible');
      } else {
        tbCount.classList.remove('visible');
        tbPrev.classList.remove('visible');
        tbNext.classList.remove('visible');
        tbClear.classList.remove('visible');
      }
    }

    function updateOverlayNav() {
      if (!ovCount || !ovPrev || !ovNext) return;
      if (searchMatches.length > 0) {
        ovCount.innerHTML = '<span class="sc-current">' + (searchMatchIdx + 1) + '</span>/' + searchMatches.length;
        ovPrev.disabled = false;
        ovNext.disabled = false;
      } else if (ovInput && ovInput.value.trim()) {
        ovCount.textContent = '0/0';
        ovPrev.disabled = true;
        ovNext.disabled = true;
      } else {
        ovCount.textContent = '';
        ovPrev.disabled = true;
        ovNext.disabled = true;
      }
    }

    function clearAllSearch() {
      clearSearchHighlights();
      currentQuery = '';
      if (tbInput) tbInput.value = '';
      if (ovInput) ovInput.value = '';
      updateAllNav();
    }

    // ── Top bar search: live search as you type ──
    if (tbInput) {
      tbInput.addEventListener('input', function() {
        var q = tbInput.value.trim();
        // Sync overlay input
        if (ovInput && ovInput.value !== tbInput.value) ovInput.value = tbInput.value;
        doSearch(q);
      });
      tbInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          navigateSearch(e.shiftKey ? -1 : 1);
        }
        if (e.key === 'Escape') {
          clearAllSearch();
          tbInput.blur();
        }
      });
    }
    if (tbPrev) tbPrev.addEventListener('click', function() { navigateSearch(-1); });
    if (tbNext) tbNext.addEventListener('click', function() { navigateSearch(1); });
    if (tbClear) tbClear.addEventListener('click', clearAllSearch);

    // ── Overlay fallback ──
    function openOverlay() {
      if (!overlay) return;
      overlay.classList.add('active');
      if (ovInput) {
        // Sync from top bar if there's a query
        if (tbInput && tbInput.value.trim()) {
          ovInput.value = tbInput.value;
        } else {
          ovInput.value = '';
        }
        ovInput.focus();
        ovInput.select();
      }
    }

    function closeOverlay() {
      if (!overlay) return;
      overlay.classList.remove('active');
    }

    if (overlay) {
      overlay.addEventListener('click', function(e) {
        if (e.target === overlay) closeOverlay();
      });
    }
    if (ovClose) ovClose.addEventListener('click', closeOverlay);
    if (ovInput) {
      ovInput.addEventListener('input', function() {
        var q = ovInput.value.trim();
        if (tbInput && tbInput.value !== ovInput.value) tbInput.value = ovInput.value;
        doSearch(q);
      });
      ovInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          navigateSearch(e.shiftKey ? -1 : 1);
        }
      });
    }
    if (ovPrev) ovPrev.addEventListener('click', function() { navigateSearch(-1); });
    if (ovNext) ovNext.addEventListener('click', function() { navigateSearch(1); });

    // ── Global keyboard shortcuts ──
    document.addEventListener('keydown', function(e) {
      var active = document.activeElement;
      var isFormField = active && (
        active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable
      );
      // Ctrl+K / Cmd+K / / → focus top bar search (primary)
      if ((e.key === 'k' && (e.metaKey || e.ctrlKey)) || e.key === '/' || (e.key === 'f' && (e.metaKey || e.ctrlKey))) {
        // If we're already in the top bar search, let the overlay open instead
        if (isFormField && active === tbInput) {
          e.preventDefault();
          openOverlay();
          return;
        }
        if (!isFormField) {
          e.preventDefault();
          clearKeywordHighlights();
          if (tbInput) { tbInput.focus(); tbInput.select(); }
        }
      }
      // Escape → clear search
      if (e.key === 'Escape') {
        if (overlay && overlay.classList.contains('active')) {
          closeOverlay();
        }
        if (currentQuery) {
          clearAllSearch();
        }
      }
    });
  }

  // ══════════════════════════════════════════
  // ══════════════════════════════════════════
  // 10. TEXT SELECTION — RESEARCH LOOKUP (划词检索)
  // ══════════════════════════════════════════
  function initTextSelectionSearch() {
    // ── Desktop Popup ──
    var popup = document.createElement('div');
    popup.className = 'text-select-popup';
    popup.id = 'text-select-popup';
    popup.innerHTML =
      '<div class="ts-selected-preview" id="ts-selected-preview"></div>' +
      '<div class="ts-actions-row">' +
      '<button class="ts-btn" id="ts-web-btn" title="联网搜索 — 查询外部信息、定义、新闻">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>' +
      'Search\u00A0Web' +
      '</button>' +
      '<span class="ts-divider"></span>' +
      '<button class="ts-btn" id="ts-internal-btn" title="在 ArgueLab 站内数据库中检索">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>' +
      'Search\u00A0ArgueLab' +
      '</button>' +
      '<span class="ts-divider"></span>' +
      '<button class="ts-btn" id="ts-explain-btn" title="学习解释 — 修辞功能 + 可迁移句型 + 例句">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>' +
      'Explain' +
      '</button>' +
      '<span class="ts-divider"></span>' +
      '<button class="ts-btn ts-btn-save" id="ts-save-btn" title="保存选中文本到笔记">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>' +
      'Save' +
      '</button>' +
      '</div>';
    document.body.appendChild(popup);

    // ── Mobile Bottom Sheet ──
    var bsOverlay = document.createElement('div');
    bsOverlay.className = 'selection-bs-overlay';
    bsOverlay.id = 'selection-bs-overlay';
    document.body.appendChild(bsOverlay);

    var bottomSheet = document.createElement('div');
    bottomSheet.className = 'selection-bottom-sheet';
    bottomSheet.id = 'selection-bottom-sheet';
    bottomSheet.innerHTML =
      '<div class="bs-drag-handle"></div>' +
      '<div class="bs-selected-text" id="bs-selected-text"></div>' +
      '<div class="bs-actions">' +
        '<button class="bs-action" id="bs-web-btn">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>' +
          'Search Web' +
        '</button>' +
        '<button class="bs-action" id="bs-internal-btn">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>' +
          'Search ArgueLab' +
        '</button>' +
        '<button class="bs-action" id="bs-explain-btn">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>' +
          'Explain' +
        '</button>' +
        '<button class="bs-action bs-action-save" id="bs-save-btn">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>' +
          'Save to Notes' +
        '</button>' +
      '</div>' +
      '<button class="bs-cancel" id="bs-cancel-btn">Cancel</button>';
    document.body.appendChild(bottomSheet);

    // ── Drawer ──
    var overlay = document.createElement('div');
    overlay.className = 'research-drawer-overlay';
    overlay.id = 'research-overlay';
    document.body.appendChild(overlay);

    var drawer = document.createElement('div');
    drawer.className = 'research-drawer';
    drawer.id = 'research-drawer';
    drawer.innerHTML =
      '<div class="rd-header">' +
        '<div class="rd-header-left">' +
          '<span class="rd-title" id="rd-title">Research Lookup</span>' +
        '</div>' +
        '<button class="rd-close" id="rd-close" title="关闭">&times;</button>' +
      '</div>' +
      '<div class="rd-body" id="rd-body">' +
        '<div class="rd-empty" id="rd-empty">' +
          '<div class="rd-empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="width:32px;height:32px;opacity:0.4"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div>' +
          '<div>Select text and click a search button to start</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(drawer);

    // ── Delegated click: data-rd-mode buttons dispatch rd-search ──
    drawer.addEventListener('click', function(e) {
      // ── Match card click → scroll to match ──
      var matchCard = e.target.closest('.rd-match-card');
      if (matchCard) {
        var idx = parseInt(matchCard.getAttribute('data-rd-match'));
        if (!isNaN(idx)) scrollToMatch(idx);
        return;
      }

      // ── Nav button click → prev/next ──
      var navBtn = e.target.closest('[data-rd-nav]');
      if (navBtn) {
        var dir = navBtn.getAttribute('data-rd-nav');
        if (dir === 'prev') navigateMatch(-1);
        else if (dir === 'next') navigateMatch(1);
        return;
      }

      var btn = e.target.closest('.rd-mode-btn');
      if (!btn) {
        // Check for copy-pattern button
        var copyBtn = e.target.closest('.rd-ex-copy-btn');
        if (copyBtn) {
          var target = copyBtn.getAttribute('data-copy-target');
          if (target === 'parent') {
            var text = copyBtn.parentElement.textContent.replace('▸ ', '').trim();
            navigator.clipboard.writeText(text).then(function() {
              showToast('已复制');
            }).catch(function() {});
          }
        }
        return;
      }
      var mode = btn.getAttribute('data-rd-mode');
      if (!mode || !selectedText) return;
      if (btn.disabled) return;
      document.dispatchEvent(new CustomEvent('rd-search', {detail: {mode: mode, query: selectedText}}));
    });

    var selectedText = '';
    var isActive = false;
    var hideTimer = null;
    var currentMode = 'internal'; // 'internal' | 'web'

    // ── Popup helpers ──
    var MOBILE_BREAKPOINT = 640;

    function isMobile() {
      return window.innerWidth <= MOBILE_BREAKPOINT;
    }

    function showPopup(x, y) {
      // Update selected text preview
      var previewEl = document.getElementById('ts-selected-preview');
      if (previewEl) {
        previewEl.textContent = (selectedText.length > 80 ? selectedText.substring(0, 80) + '\u2026' : selectedText);
      }

      // Mobile: show bottom sheet
      if (isMobile()) {
        showBottomSheet();
        return;
      }

      // Desktop: position floating popup — uses position: fixed (viewport-relative)
      var pw = popup.offsetWidth;
      var ph = popup.offsetHeight;
      var ww = window.innerWidth;
      var padding = 12;
      var left = Math.max(padding, Math.min(x - pw / 2, ww - pw - padding));
      var top = y - ph - 16;
      if (top < 8) { top = y + 22; }
      popup.style.left = left + 'px';
      popup.style.top = top + 'px';
      popup.classList.add('active');
      isActive = true;
    }

    function hidePopup() {
      popup.classList.remove('active');
      isActive = false;
      selectedText = '';
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
      // Also hide bottom sheet if it's open
      if (bsActive) hideBottomSheet();
    }

    // ── Bottom sheet helpers ──
    var bsActive = false;

    function showBottomSheet() {
      var previewEl = document.getElementById('bs-selected-text');
      if (previewEl) {
        previewEl.textContent = selectedText;
      }
      bsOverlay.classList.add('active');
      bottomSheet.classList.add('active');
      bsActive = true;
      isActive = true;
    }

    function hideBottomSheet() {
      bsOverlay.classList.remove('active');
      bottomSheet.classList.remove('active');
      bsActive = false;
    }

    // ── In-page search ──
    var _matchNodes = [];
    var _currentMatchIndex = -1;

    function escapeRegex(str) {
      return str.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
    }

    function searchInPage(query) {
      clearHighlights();
      var root = document.querySelector('.issue-main');
      if (!root) root = document.body;

      var matches = [];
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
      var node;
      var qRe = new RegExp(escapeRegex(query), 'gi');
      var globalIdx = 0;

      while (node = walker.nextNode()) {
        var parent = node.parentElement;
        if (!parent || parent.closest('#research-drawer, #research-overlay, #text-select-popup, #selection-bottom-sheet, #selection-bs-overlay, script, style, noscript, .rd-highlight')) {
          continue;
        }

        var text = node.textContent;
        var m;
        qRe.lastIndex = 0;

        while (m = qRe.exec(text)) {
          var ctxStart = Math.max(0, m.index - 55);
          var ctxEnd = Math.min(text.length, m.index + m[0].length + 55);
          var ctx = (ctxStart > 0 ? '\u2026' : '') + text.substring(ctxStart, ctxEnd) + (ctxEnd < text.length ? '\u2026' : '');

          matches.push({
            node: node,
            startOffset: m.index,
            endOffset: m.index + m[0].length,
            text: m[0],
            index: globalIdx,
            context: ctx
          });
          globalIdx++;
        }
      }

      highlightMatches(matches);
      _currentMatchIndex = -1;
      return matches;
    }

    function highlightMatches(matches) {
      _matchNodes = [];
      // Process in reverse order to preserve offsets
      for (var i = matches.length - 1; i >= 0; i--) {
        var m = matches[i];
        try {
          var range = document.createRange();
          range.setStart(m.node, m.startOffset);
          range.setEnd(m.node, m.endOffset);
          var mark = document.createElement('mark');
          mark.className = 'rd-highlight';
          mark.setAttribute('data-rd-match-index', i);
          range.surroundContents(mark);
          _matchNodes.unshift(mark);
          // Store reference on the match object
          m.markEl = mark;
        } catch(e) {
          // Cross-element boundary — skip
        }
      }
      // Rebuild _matchNodes in forward order
      _matchNodes = document.querySelectorAll('.rd-highlight[data-rd-match-index]');
      // Sort by DOM order (data attribute stores reverse index, rebase)
      _matchNodes = Array.prototype.slice.call(_matchNodes).sort(function(a, b) {
        return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1;
      });
      for (var j = 0; j < _matchNodes.length; j++) {
        _matchNodes[j].setAttribute('data-rd-match-index', j);
      }
    }

    function clearHighlights() {
      var marks = document.querySelectorAll('.rd-highlight');
      for (var i = marks.length - 1; i >= 0; i--) {
        var mark = marks[i];
        var parent = mark.parentNode;
        if (parent) {
          while (mark.firstChild) {
            parent.insertBefore(mark.firstChild, mark);
          }
          parent.removeChild(mark);
        }
      }
      _matchNodes = [];
      _currentMatchIndex = -1;
      // Merge adjacent text nodes that may have been split
      document.body.normalize();
    }

    function scrollToMatch(index) {
      if (!_matchNodes || _matchNodes.length === 0) return;
      if (index < 0) index = 0;
      if (index >= _matchNodes.length) index = _matchNodes.length - 1;

      // Deactivate previous
      if (_currentMatchIndex >= 0 && _currentMatchIndex < _matchNodes.length) {
        _matchNodes[_currentMatchIndex].classList.remove('rd-highlight-active');
      }

      _currentMatchIndex = index;
      var mark = _matchNodes[index];
      mark.classList.add('rd-highlight-active');
      mark.scrollIntoView({ behavior: 'smooth', block: 'center' });

      // Update drawer match cards
      var prevCard = document.querySelector('.rd-match-card.active');
      if (prevCard) prevCard.classList.remove('active');
      var cards = document.querySelectorAll('.rd-match-card[data-rd-match]');
      for (var c = 0; c < cards.length; c++) {
        if (parseInt(cards[c].getAttribute('data-rd-match')) === index) {
          cards[c].classList.add('active');
          cards[c].scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          break;
        }
      }
    }

    function navigateMatch(direction) {
      if (_matchNodes.length === 0) return;
      var next = _currentMatchIndex + direction;
      if (next < 0) next = _matchNodes.length - 1;
      if (next >= _matchNodes.length) next = 0;
      scrollToMatch(next);
    }

    // ── Drawer helpers ──
    function openDrawer(query, mode) {
      currentMode = mode;
      var titleEl = document.getElementById('rd-title');
      if (titleEl) {
        var label = mode === 'web' ? '联网搜索' : '站内检索';
        titleEl.innerHTML = label + ': <span style="color:var(--accent);font-weight:600">' + escapeHTML(query) + '</span>';
      }
      document.getElementById('research-drawer').classList.add('active');
      document.getElementById('research-overlay').classList.add('active');
      document.body.style.overflow = 'hidden';
    }

    function closeDrawer() {
      document.getElementById('research-drawer').classList.remove('active');
      document.getElementById('research-overlay').classList.remove('active');
      document.body.style.overflow = '';
      clearHighlights();
    }

    function escapeHTML(str) {
      var div = document.createElement('div');
      div.textContent = str;
      return div.innerHTML;
    }

    function showLoading() {
      var body = document.getElementById('rd-body');
      body.innerHTML = '<div class="rd-loading"><div class="rd-spinner"></div><span>Searching…</span></div>';
    }

    function showEmpty(msg) {
      var body = document.getElementById('rd-body');
      body.innerHTML = '<div class="rd-empty"><div class="rd-empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="width:32px;height:32px;opacity:0.4"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div><div>' + (msg || 'No results found') + '</div></div>';
    }

    function renderInPageResults(matches, query) {
      clearHighlights();
      var body = document.getElementById('rd-body');
      var html = '';
      var webDisabled = (typeof ARGUELAB_WEB_ENABLED !== 'undefined' && !ARGUELAB_WEB_ENABLED);
      var webTitle = webDisabled ? 'Web search is not configured' : '外部网络搜索';
      var webClass = webDisabled ? ' rd-mode-disabled' : '';

      // Mode switch: In-page (active) | Knowledge Base | Web
      html += '<div class="rd-mode-switch">' +
        '<button class="rd-mode-btn active" data-rd-mode="internal">' +
        'In-page (' + matches.length + ')</button>' +
        '<button class="rd-mode-btn" data-rd-mode="kb">Knowledge Base</button>' +
        '<button class="rd-mode-btn' + webClass + '" data-rd-mode="web" title="' + webTitle + '"' + (webDisabled ? ' disabled' : '') + '>Web</button>' +
        '</div>';

      if (matches.length === 0) {
        html += '<div class="rd-empty"><div class="rd-empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="width:32px;height:32px;opacity:0.4"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div><div>No matches found on this page. Try Knowledge Base.</div></div>';
        body.innerHTML = html;
        return;
      }

      // Nav bar: count + prev/next
      html += '<div class="rd-nav-bar">' +
        '<span class="rd-nav-count">' + matches.length + ' match' + (matches.length > 1 ? 'es' : '') + ' on this page</span>' +
        '<button class="rd-nav-btn" data-rd-nav="prev" title="Prev (Shift+Enter)">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>' +
        '</button>' +
        '<button class="rd-nav-btn" data-rd-nav="next" title="Next (Enter)">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>' +
        '</button>' +
        '</div>';

      // Match cards
      var escapedQ = escapeHTML(query);
      for (var i = 0; i < matches.length; i++) {
        var ctx = escapeHTML(matches[i].context);
        var qRe = new RegExp('(' + query.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
        ctx = ctx.replace(qRe, '<em>$1</em>');
        html += '<div class="rd-match-card" data-rd-match="' + i + '">' +
          '<div class="rd-match-index">#' + (i + 1) + '</div>' +
          '<div class="rd-match-context">' + ctx + '</div>' +
          '</div>';
      }

      body.innerHTML = html;
    }

    function renderKnowledgeBaseResults(data) {
      var body = document.getElementById('rd-body');
      var html = '';
      var webDisabled = (typeof ARGUELAB_WEB_ENABLED !== 'undefined' && !ARGUELAB_WEB_ENABLED);
      var webTitle = webDisabled ? 'Web search is not configured' : '外部网络搜索';
      var webClass = webDisabled ? ' rd-mode-disabled' : '';

      // Mode switch: In-page | Knowledge Base (active) | Web
      html += '<div class="rd-mode-switch">' +
        '<button class="rd-mode-btn" data-rd-mode="internal">In-page</button>' +
        '<button class="rd-mode-btn active" data-rd-mode="kb">Knowledge Base ' + (data.total ? '(' + data.total + ')' : '') + '</button>' +
        '<button class="rd-mode-btn' + webClass + '" data-rd-mode="web" title="' + webTitle + '"' + (webDisabled ? ' disabled' : '') + '>Web</button>' +
        '</div>';

      if (!data.results || data.results.length === 0) {
        html += '<div class="rd-empty"><div class="rd-empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="width:32px;height:32px;opacity:0.4"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div><div>No cross-issue matches. Try In-page or Web search.</div></div>';
        body.innerHTML = html;
        return;
      }

      for (var i = 0; i < data.results.length; i++) {
        var r = data.results[i];
        var tagClass = 'tag-' + r.section_type;
        var tagLabel = r.section_heading.replace(/^[#\\d.\\s]*/, '').substring(0, 12);
        var snippet = escapeHTML(r.snippet);
        // Highlight query
        var qry = escapeHTML(selectedText);
        var re = new RegExp('(' + qry.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&') + ')', 'gi');
        snippet = snippet.replace(re, '<em>$1</em>');

        html +=
          '<a class="rd-result-card" href="' + r.url + '" target="_top">' +
            '<div class="rd-result-meta">' +
              '<span class="rd-section-tag ' + tagClass + '">' + tagLabel + '</span>' +
              '<span class="rd-date">' + escapeHTML(r.date) + '</span>' +
            '</div>' +
            '<div class="rd-snippet">' + snippet + '</div>' +
          '</a>';
      }

      body.innerHTML = html;
    }

    function renderWebResults(data) {
      var body = document.getElementById('rd-body');
      var html = '';
      var webDisabled = (typeof ARGUELAB_WEB_ENABLED !== 'undefined' && !ARGUELAB_WEB_ENABLED);
      var webTitle = webDisabled ? 'Web search is not configured' : '外部网络搜索';
      var webClass = webDisabled ? ' rd-mode-disabled' : '';

      // Mode switch: In-page | Knowledge Base | Web (active)
      html += '<div class="rd-mode-switch">' +
        '<button class="rd-mode-btn" data-rd-mode="internal">In-page</button>' +
        '<button class="rd-mode-btn" data-rd-mode="kb">Knowledge Base</button>' +
        '<button class="rd-mode-btn active' + webClass + '" data-rd-mode="web" title="' + webTitle + '"' + (webDisabled ? ' disabled' : '') + '>Web</button>' +
        '</div>';

      if (!data.results || data.results.length === 0) {
        html += '<div class="rd-empty"><div class="rd-empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="width:32px;height:32px;opacity:0.4"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div><div>No web results found</div></div>';
        body.innerHTML = html;
        return;
      }

      for (var i = 0; i < data.results.length; i++) {
        var r = data.results[i];
        var snip = escapeHTML(r.snippet);
        var title = escapeHTML(r.title);
        var isExternal = r.type === 'external_link';
        var isAbstract = r.type === 'abstract';
        var urlShow = r.url ? r.url.replace(/^https?:\\/\\//, '').replace(/\\/$/, '').substring(0, 60) : '';

        var tagHtml = isExternal
          ? '<span class="rd-section-tag tag-web" style="background:rgba(245,158,11,0.12);color:#f59e0b">OPEN IN DDG</span>'
          : isAbstract
            ? '<span class="rd-section-tag tag-web">DEFINITION</span>'
            : '<span class="rd-section-tag tag-web">WEB</span>';

        var target = isExternal ? '_blank' : '_blank';
        var rel = isExternal ? 'noopener' : 'noopener';

        html +=
          '<a class="rd-result-card' + (isExternal ? ' rd-external-link' : '') + '" href="' + (r.url ? escapeHTML(r.url) : '#') + '" target="' + target + '" rel="' + rel + '">' +
            '<div class="rd-result-meta">' + tagHtml + '</div>' +
            '<div class="rd-snippet"' + (isExternal ? ' style="font-weight:600;color:var(--ink);font-size:13px"' : '') + '>' + title + '</div>' +
            (r.type !== 'external_link' ? '<div class="rd-snippet" style="margin-top:4px">' + snip + '</div>' +
            '<div class="rd-web-url">' + escapeHTML(urlShow) + '</div>' : '') +
          '</a>';
      }

      body.innerHTML = html;
    }

    function renderExplainResults(data) {
      var body = document.getElementById('rd-body');
      var query = escapeHTML(data.selectedText || selectedText);
      var html = '';
      var webDisabled = (typeof ARGUELAB_WEB_ENABLED !== 'undefined' && !ARGUELAB_WEB_ENABLED);
      var webClass = webDisabled ? ' rd-mode-disabled' : '';
      // Mode switch with explain tab active
      html += '<div class="rd-mode-switch">' +
        '<button class="rd-mode-btn" data-rd-mode="internal">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:13px;vertical-align:-2px;margin-right:3px"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>' +
        'In-site</button>' +
        '<button class="rd-mode-btn' + webClass + '" data-rd-mode="web"' + (webDisabled ? ' disabled style="opacity:0.4;cursor:not-allowed"' : '') + ' title="' + (webDisabled ? 'Web search is not configured' : 'Web search') + '">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:13px;vertical-align:-2px;margin-right:3px"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>' +
        'Web' + (webDisabled ? ' (off)' : '') + '</button>' +
        '<button class="rd-mode-btn active">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:13px;height:13px;vertical-align:-2px;margin-right:3px"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>' +
        'Explain</button>' +
        '</div>';

      // Explain card
      html += '<div class="rd-explain-card">';

      // Plain meaning
      html += '<div class="rd-ex-section">';
      html += '<div class="rd-ex-label">中文释义</div>';
      html += '<div class="rd-ex-text">' + escapeHTML(data.plainMeaning || '') + '</div>';
      html += '</div>';

      // Argument function
      html += '<div class="rd-ex-section">';
      html += '<div class="rd-ex-label">Argument Function</div>';
      html += '<div class="rd-ex-text rd-ex-fn">' + escapeHTML(data.argumentFunction || '') + '</div>';
      html += '</div>';

      // Chinese explanation
      html += '<div class="rd-ex-section">';
      html += '<div class="rd-ex-label">中文解析</div>';
      html += '<div class="rd-ex-text">' + escapeHTML(data.chineseExplanation || '') + '</div>';
      html += '</div>';

      // Reusable patterns
      if (data.reusablePatterns && data.reusablePatterns.length > 0) {
        html += '<div class="rd-ex-section">';
        html += '<div class="rd-ex-label">可迁移句型 (' + data.reusablePatterns.length + ')</div>';
        for (var i = 0; i < data.reusablePatterns.length; i++) {
          html += '<div class="rd-ex-pattern"><span class="rd-ex-bullet">▸</span> ' + escapeHTML(data.reusablePatterns[i]) +
            '<button class="rd-ex-copy-btn" data-copy-target="parent" title="复制此句型"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>' +
            '</div>';
            '</div>';
        }
        html += '</div>';
      }

      // Example
      if (data.example) {
        html += '<div class="rd-ex-section">';
        html += '<div class="rd-ex-label">示例</div>';
        html += '<div class="rd-ex-text rd-ex-example"><em>' + escapeHTML(data.example) + '</em></div>';
        html += '</div>';
      }

      // Source badge
      var sourceLabel = data.source === 'built-in' ? 'Built-in Dictionary' : data.source === 'fuzzy-match' ? 'Fuzzy Match' : 'Template';
      html += '<div class="rd-ex-source-tag">Source: ' + sourceLabel + '</div>';

      html += '</div>'; // close rd-explain-card

      body.innerHTML = html;
    }

    // ── Explain lookup ──
    function doExplainLookup(text) {
      if (!text) return;
      selectedText = text;
      var titleEl = document.getElementById('rd-title');
      if (titleEl) {
        titleEl.innerHTML = '学习解释: <span style="color:var(--accent);font-weight:600">' + escapeHTML(text) + '</span>';
      }
      document.getElementById('research-drawer').classList.add('active');
      document.getElementById('research-overlay').classList.add('active');
      document.body.style.overflow = 'hidden';

      var body = document.getElementById('rd-body');
      body.innerHTML = '<div class="rd-loading"><div class="rd-spinner"></div><span>Fetching explanation…</span></div>';

      fetch('/api/lookup/explain', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ selectedText: text, context: { issueId: window.location.pathname.split('/').pop() || '' } })
      })
        .then(function(resp) {
          if (!resp.ok) throw new Error('Explain lookup failed');
          return resp.json();
        })
        .then(function(data) {
          renderExplainResults(data);
        })
        .catch(function(err) {
          showEmpty('解释请求失败，请检查网络后重试');
        });
    }

    // ── Search dispatch ──
    function doResearchLookup(query, mode) {
      selectedText = query;
      if (!query) return;

      if (mode === 'internal') {
        // In-page DOM search: highlight matches and show in drawer
        openDrawer(query, 'internal');
        var matches = searchInPage(query);
        renderInPageResults(matches, query);
        return;
      }

      openDrawer(query, mode);
      showLoading();

      var endpoint = mode === 'web'
        ? '/api/search/web?q=' + encodeURIComponent(query) + '&limit=10'
        : '/api/search/internal?q=' + encodeURIComponent(query) + '&limit=15';

      fetch(endpoint)
        .then(function(resp) {
          if (!resp.ok) throw new Error('Search failed');
          return resp.json();
        })
        .then(function(data) {
          if (mode === 'web') {
            renderWebResults(data);
          } else {
            renderKnowledgeBaseResults(data);
          }
        })
        .catch(function(err) {
          showEmpty('搜索请求失败，请检查网络后重试');
        });
    }

    // ── Custom event listener for mode switching inside drawer ──
    document.addEventListener('rd-search', function(e) {
      doResearchLookup(e.detail.query, e.detail.mode);
    });

    // ── Shared selection helpers ──
    var SEL_MIN_LEN = 2;
    var SEL_MAX_LEN = 120;

    function _shouldExcludeSelection(el) {
      if (!el) return false;
      return !!(el.closest('#text-select-popup') ||
                el.closest('#selection-bottom-sheet') ||
                el.closest('#selection-bs-overlay') ||
                el.closest('#top-search-input') ||
                el.closest('#search-input') ||
                el.closest('#research-drawer') ||
                el.closest('#research-overlay') ||
                el.closest('input') ||
                el.closest('textarea'));
    }

    function _extractAndValidate() {
      var sel = window.getSelection();
      if (!sel || sel.isCollapsed) return null;
      var text = sel.toString().trim();
      if (!text || text.length < SEL_MIN_LEN || text.length > SEL_MAX_LEN) return null;
      if (/^[\\s]+$/.test(text)) return null;
      return { text: text, sel: sel };
    }

    // ── Selection detection: mouseup (primary, quick response) ──
    document.addEventListener('mouseup', function(e) {
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
      setTimeout(function() {
        if (_shouldExcludeSelection(e.target)) return;
        var extracted = _extractAndValidate();
        if (!extracted) { hidePopup(); return; }

        var range;
        try { range = extracted.sel.getRangeAt(0); } catch(err) { return; }
        var rect = range.getBoundingClientRect();
        var cx = rect.left + rect.width / 2;
        var cy = rect.top;

        selectedText = extracted.text;
        showPopup(cx, cy);
      }, 15);
    });

    // ── Selection detection: selectionchange (debounced, catches keyboard selection) ──
    var _selChangeTimer = null;
    document.addEventListener('selectionchange', function() {
      if (_selChangeTimer) { clearTimeout(_selChangeTimer); }
      _selChangeTimer = setTimeout(function() {
        var sel = window.getSelection();
        // If selection is inside an excluded zone, hide and bail
        if (sel && sel.rangeCount > 0) {
          var range = sel.getRangeAt(0);
          var container = range.commonAncestorContainer;
          var el = container.nodeType === 3 ? container.parentElement : container;
          if (_shouldExcludeSelection(el)) { hidePopup(); return; }
        }

        var extracted = _extractAndValidate();
        if (!extracted) {
          // Only hide if currently active (don't hide drawer on every click)
          if (isActive && !document.getElementById('research-drawer').classList.contains('active')) {
            hidePopup();
          }
          return;
        }

        // Don't re-show if already active with the same text (avoids flicker)
        if (isActive && selectedText === extracted.text) return;

        var range2;
        try { range2 = extracted.sel.getRangeAt(0); } catch(err) { return; }
        var rect = range2.getBoundingClientRect();
        var cx = rect.left + rect.width / 2;
        var cy = rect.top;

        selectedText = extracted.text;
        showPopup(cx, cy);
      }, 200);
    });

    // ── Save to Notes ──
    function saveToNotes(text) {
      if (!text || !text.trim()) return;
      try {
        var notes = [];
        var stored = localStorage.getItem('arguelab-saved-notes');
        if (stored) {
          notes = JSON.parse(stored);
        }
        // Detect which section the selection is in
        var sectionHeading = '';
        var sel = window.getSelection();
        if (sel && sel.rangeCount > 0) {
          var node = sel.getRangeAt(0).commonAncestorContainer;
          var section = node.nodeType === 3 ? node.parentElement.closest('.issue-section') : node.closest('.issue-section');
          if (section) {
            var h2 = section.querySelector('.section-heading h2');
            if (h2) sectionHeading = h2.textContent.trim();
          }
        }
        notes.push({
          id: Date.now(),
          text: text.trim(),
          issueDate: typeof ISSUE_DATE !== 'undefined' ? ISSUE_DATE : '',
          sectionHeading: sectionHeading,
          savedAt: new Date().toISOString()
        });
        localStorage.setItem('arguelab-saved-notes', JSON.stringify(notes));
        if (typeof showToast === 'function') {
          showToast('Saved to notes \u2713');
        }
      } catch(e) {
        if (typeof showToast === 'function') {
          showToast('Save failed');
        }
      }
    }

    // ── Desktop Button handlers ──
    // Reordered: Web Search → Search ArgueLab → Explain → Save
    document.getElementById('ts-web-btn').addEventListener('mousedown', function(e) {
      e.preventDefault();
      if (!ARGUELAB_WEB_ENABLED) {
        if (typeof showToast === 'function') {
          showToast('Web search is not configured. Use In-site or Explain instead.');
        }
        hidePopup();
        return;
      }
      if (selectedText) { doResearchLookup(selectedText, 'web'); }
      hidePopup();
    });

    document.getElementById('ts-internal-btn').addEventListener('mousedown', function(e) {
      e.preventDefault();
      if (selectedText) { doResearchLookup(selectedText, 'internal'); }
      hidePopup();
    });

    document.getElementById('ts-explain-btn').addEventListener('mousedown', function(e) {
      e.preventDefault();
      if (selectedText) { doExplainLookup(selectedText); }
      hidePopup();
    });

    document.getElementById('ts-save-btn').addEventListener('mousedown', function(e) {
      e.preventDefault();
      if (selectedText) {
        saveToNotes(selectedText);
        var btn = document.getElementById('ts-save-btn');
        if (btn) {
          var origHTML = btn.innerHTML;
          btn.innerHTML =
            '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none" style="width:13px;height:13px;flex-shrink:0"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>' +
            'Saved';
          setTimeout(function() {
            btn.innerHTML = origHTML;
          }, 1200);
        }
      }
      hidePopup();
    });

    // ── Mobile Bottom Sheet Button handlers ──
    document.getElementById('bs-web-btn').addEventListener('click', function(e) {
      e.preventDefault();
      if (!ARGUELAB_WEB_ENABLED) {
        if (typeof showToast === 'function') {
          showToast('Web search is not configured.');
        }
        hideBottomSheet();
        hidePopup();
        return;
      }
      if (selectedText) { doResearchLookup(selectedText, 'web'); }
      hideBottomSheet();
      hidePopup();
    });

    document.getElementById('bs-internal-btn').addEventListener('click', function(e) {
      e.preventDefault();
      if (selectedText) { doResearchLookup(selectedText, 'internal'); }
      hideBottomSheet();
      hidePopup();
    });

    document.getElementById('bs-explain-btn').addEventListener('click', function(e) {
      e.preventDefault();
      if (selectedText) { doExplainLookup(selectedText); }
      hideBottomSheet();
      hidePopup();
    });

    document.getElementById('bs-save-btn').addEventListener('click', function(e) {
      e.preventDefault();
      if (selectedText) { saveToNotes(selectedText); }
      hideBottomSheet();
      hidePopup();
    });

    document.getElementById('bs-cancel-btn').addEventListener('click', function(e) {
      e.preventDefault();
      hideBottomSheet();
      hidePopup();
    });

    // Bottom sheet overlay click to dismiss
    bsOverlay.addEventListener('click', function(e) {
      if (e.target === bsOverlay) {
        hideBottomSheet();
        hidePopup();
      }
    });

    // ── Drawer close ──
    document.getElementById('rd-close').addEventListener('click', closeDrawer);
    document.getElementById('research-overlay').addEventListener('click', closeDrawer);

    // ── Dismiss popup (desktop + mobile) ──
    document.addEventListener('mousedown', function(e) {
      if (isActive && !e.target.closest('#text-select-popup') &&
          !e.target.closest('#selection-bottom-sheet') &&
          !e.target.closest('#selection-bs-overlay') &&
          !e.target.closest('#research-drawer')) {
        hideTimer = setTimeout(hidePopup, 50);
      }
    });

    // Touch-based selection for mobile
    document.addEventListener('touchend', function(e) {
      // Don't trigger if popup/sheet is already active or user is interacting with it
      if (isActive) return;
      if (_shouldExcludeSelection(e.target)) return;
      setTimeout(function() {
        var extracted = _extractAndValidate();
        if (!extracted) { hidePopup(); return; }
        selectedText = extracted.text;
        showPopup(0, 0); // position doesn't matter on mobile — bottom sheet handles it
      }, 300); // longer delay to let selection stabilize on mobile
    });

    document.addEventListener('keydown', function(e) {
      var isDrawerOpen = document.getElementById('research-drawer').classList.contains('active');
      if (e.key === 'Escape') {
        if (isDrawerOpen) {
          closeDrawer();
        } else if (isActive) {
          hidePopup();
        }
        return;
      }
      // ── Drawer keyboard nav: Enter = next, Shift+Enter = prev ──
      if (isDrawerOpen && _matchNodes && _matchNodes.length > 0 && e.key === 'Enter') {
        e.preventDefault();
        if (e.shiftKey) {
          navigateMatch(-1);
        } else {
          navigateMatch(1);
        }
      }
    });

    var scrollTimeout;
    window.addEventListener('scroll', function() {
      if (isActive) {
        clearTimeout(scrollTimeout);
        scrollTimeout = setTimeout(hidePopup, 80);
      }
    }, { passive: true });

    window.addEventListener('resize', function() {
      if (isActive) hidePopup();
    });
  }

  // ══════════════════════════════════════════
  // 11. KEYWORD RELATION POPOVER
  // ══════════════════════════════════════════
  var kwPopover = null;

  function initKeywordPopover() {
    // Build keyword data: map expression terms to their CN definitions and related expressions
    var kwData = {};
    document.querySelectorAll('.expr-card').forEach(function(card) {
      var phraseEl = card.querySelector('.expr-phrase');
      var cnEl = card.querySelector('.expr-cn');
      var tagsEl = card.querySelector('.expr-tags');
      if (phraseEl) {
        var phrase = phraseEl.textContent.trim();
        var cn = cnEl ? cnEl.textContent.trim() : '';
        var tags = tagsEl ? tagsEl.textContent.trim() : '';
        kwData[phrase] = { cn: cn, tags: tags, phrase: phrase };
      }
    });

    // Collect all related phrases for cross-referencing
    var allPhrases = Object.keys(kwData);

    // Create popover element
    kwPopover = document.createElement('div');
    kwPopover.className = 'kw-popover';
    document.body.appendChild(kwPopover);

    // Show popover on keyword chip hover
    document.querySelectorAll('.kt-chip').forEach(function(chip) {
      var kw = chip.getAttribute('data-kw');
      var data = kwData[kw];
      if (!data) {
        // Try partial matching
        for (var i = 0; i < allPhrases.length; i++) {
          if (allPhrases[i].indexOf(kw) >= 0 || kw.indexOf(allPhrases[i]) >= 0) {
            data = kwData[allPhrases[i]];
            break;
          }
        }
      }

      chip.addEventListener('mouseenter', function(e) {
        if (!data) return;
        var rect = chip.getBoundingClientRect();
        var relatedPhrases = allPhrases.filter(function(p) {
          return p !== data.phrase && (p.indexOf(data.phrase.split(' ')[0]) >= 0 || data.phrase.indexOf(p.split(' ')[0]) >= 0);
        });

        var html = '<div class="kwp-term">' + escHtml(data.phrase) + '</div>';
        if (data.cn) {
          html += '<div class="kwp-def">' + escHtml(data.cn.substring(0, 200)) + '</div>';
        }
        if (data.tags) {
          html += '<div style="font-size:11px;color:var(--color-expression);margin-bottom:4px;">' + escHtml(data.tags) + '</div>';
        }
        if (relatedPhrases.length > 0) {
          html += '<div class="kwp-related"><strong>Related in this issue:</strong><br>' +
            relatedPhrases.slice(0, 3).map(function(p) { return escHtml(p); }).join('<br>') + '</div>';
        }

        kwPopover.innerHTML = html;
        kwPopover.style.left = Math.min(rect.left, window.innerWidth - 340) + 'px';
        kwPopover.style.top = (rect.bottom + 8) + 'px';
        kwPopover.classList.add('show');
      });

      chip.addEventListener('mouseleave', function() {
        kwPopover.classList.remove('show');
      });
    });

    // Hide popover when clicking elsewhere
    document.addEventListener('click', function(e) {
      if (!e.target.closest('.kt-chip')) {
        if (kwPopover) kwPopover.classList.remove('show');
      }
    });
  }

  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ══════════════════════════════════════════
  // 11. KEYWORDS PANEL SHOW-ALL + TOGGLE
  // ══════════════════════════════════════════
  function initKeywordsPanel() {
    // Show-all / Show-less toggle
    var showAllBtn = document.getElementById('kw-show-all');
    if (showAllBtn) {
      var expanded = false;
      showAllBtn.addEventListener('click', function() {
        expanded = !expanded;
        var chips = document.querySelectorAll('.keywords-panel .kt-chip[data-collapsed]');
        chips.forEach(function(c) { c.classList.toggle('kw-collapsed', !expanded); });
        showAllBtn.textContent = expanded ? 'Show less' : 'Show all (' + chips.length + ')';
        showAllBtn.setAttribute('aria-expanded', expanded ? 'true' : 'false');
      });
    }

    // Toolbar keywords toggle button
    var toggleBtn = document.getElementById('btn-toggle-kw');
    var panel = document.getElementById('keywords-panel');
    if (toggleBtn && panel) {
      toggleBtn.addEventListener('click', function() {
        var hidden = panel.style.display === 'none';
        panel.style.display = hidden ? '' : 'none';
        toggleBtn.classList.toggle('active', hidden);
      });
    }
  }

  // ══════════════════════════════════════════
  // 12. APP CONFIG (fetched from backend)
  // ══════════════════════════════════════════
  var ARGUELAB_WEB_ENABLED = true; // default, updated by fetch

  function fetchAppConfig() {
    fetch('/api/config')
      .then(function(r) { if (r.ok) return r.json(); throw new Error('Config unavailable'); })
      .then(function(cfg) {
        ARGUELAB_WEB_ENABLED = !!cfg.web_search_enabled;
        // Update web search popup button state
        var webBtn = document.getElementById('ts-web-btn');
        if (webBtn && !ARGUELAB_WEB_ENABLED) {
          webBtn.classList.add('ts-btn-disabled');
          webBtn.title = 'Web search is not configured — contact admin to enable';
          webBtn.style.opacity = '0.4';
          webBtn.style.cursor = 'not-allowed';
        }
      })
      .catch(function() {
        // If config fetch fails, leave web search enabled by default
        ARGUELAB_WEB_ENABLED = true;
      });
  }

  // ══════════════════════════════════════════
  // 13. INITIALIZE ALL
  // ══════════════════════════════════════════
  function init() {
    fetchAppConfig();
    injectCopyButtons();
    initChecklist();
    initGrammarToggle();
    initSectionTracking();
    initArgTooltips();
    initReadingTimer();
    initKeywordToolbar();
    initInPageSearch();
    initTextSelectionSearch();
    initKeywordPopover();
    initKeywordsPanel();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
</script>
"""

    # ── Argument Chain Demo HTML ──
    arg_demo_html = f"""
    <section class="arg-demo-issue" id="arg-demo-issue">
      <div class="demo-heading">
        <span class="source-badge sb-user" style="display:inline-block;margin-bottom:10px;">USER PRACTICE</span>
        <h2>中文观点 → 英文论证链</h2>
        <p class="demo-sub">输入一个与本期议题相关的中文观点，查看 AI 拆解为 core concepts、causal chain、impact weighing 和 academic paragraph 的过程。当前使用示例输出——后续将对接 <code>/api/argument-chain</code> 实时生成。</p>
      </div>

      <div class="adi-layout">
        <div class="adi-input">
          <h3>你的中文观点</h3>
          <p class="input-sub">用中文写出你想论证的核心观点。越具体越好——建议关联本期议题。</p>
          <textarea class="adi-textarea" id="adi-ta" rows="4"
            placeholder="例如：本期的议题让我想到……我同意/不同意文中的某个观点，因为……"
          ></textarea>
          <div class="adi-actions">
            <button class="adi-btn primary" id="adi-run" onclick="runArgDemoIssue()">
              <svg viewBox="0 0 16 16" width="13" height="13" style="fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round"><polygon points="3,2 13,8 3,14"/></svg>
              生成论证链
            </button>
            <button class="adi-btn secondary" id="adi-reset" onclick="resetArgDemoIssue()">重置</button>
          </div>
          <p class="adi-hint">
            <svg viewBox="0 0 16 16" width="12" height="12" style="display:inline-block;vertical-align:-2px;fill:none;stroke:var(--accent);stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round;margin-right:3px;"><circle cx="8" cy="8" r="3"/><path d="M8 2V1M8 15v-1M2.5 4.5l-.7-.7M14.2 12.2l-.7-.7M1 8H0M16 8h-1M2.5 11.5l-.7.7M14.2 3.8l-.7.7"/></svg>
            提示：也可以在页面中选中任意英文表达，使用右上角搜索框查找解释
          </p>
        </div>

        <div class="adi-output" id="adi-output">
          <div class="adi-placeholder" id="adi-placeholder">
            <svg viewBox="0 0 24 24" fill="none"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
            <p>点击「生成论证链」查看 AI 分析结果</p>
            <p class="hint">引擎将依次展示：Core Concepts → Causal Chain → Impact Weighing → Academic Paragraph</p>
          </div>
        </div>
      </div>
    </section>"""

    # ── Argument Chain Demo JS ──
    arg_demo_js = f"""
<script>
var ARG_DEMO_API_ISSUE = null; // Future: '/api/argument-chain'

var MOCK_ARG_OUTPUT_ISSUE = {{
  concepts: [
    {{ en: 'Algorithmic curation', zh: '算法策展' }},
    {{ en: 'Attention economy', zh: '注意力经济' }},
    {{ en: 'Deep reading atrophy', zh: '深度阅读能力萎缩' }},
    {{ en: 'Involuntary cognitive cost', zh: '非自愿认知代价' }},
    {{ en: 'Critical thinking erosion', zh: '批判性思维侵蚀' }}
  ],
  chain: [
    'Engagement-driven algorithms',
    'Fragment content exposure',
    'Reduced sustained attention',
    'Diminished deep reading capacity',
    'Weakened critical thinking'
  ],
  weighing: 'The core issue is <strong>not technological determinism but commercial design</strong>: the harm to deep reading is a <strong>byproduct of profit-maximizing algorithms</strong>, not an inevitable consequence of digital media. Students do not &ldquo;choose&rdquo; short-form content — the platform engineers an environment where deep reading becomes increasingly effortful. The <strong>asymmetry</strong> lies in who designs the choice architecture and who bears the cognitive cost.',
  paragraph: '<em>Social media platforms, through engagement-optimized algorithmic curation, are systematically eroding the deep-reading capacity and critical-thinking faculties of the younger generation. This is not, however, a voluntary trade-off — users do not freely exchange attention span for convenience. Rather, it is an involuntary cognitive cost imposed by the commercial logic of the attention economy, in which profit-maximizing algorithms engineer a choice architecture that makes sustained, reflective reading increasingly effortful. The result is a generation trained to scan, react, and move on — ill-equipped for the kind of deliberative reasoning that democratic citizenship and academic inquiry alike demand.</em>'
}};

function runArgDemoIssue() {{
  var outputEl = document.getElementById('adi-output');
  var inputText = document.getElementById('adi-ta').value.trim();
  var runBtn = document.getElementById('adi-run');

  if (!inputText) {{
    alert('Please enter your viewpoint first. / 请先输入你的中文观点。');
    return;
  }}

  runBtn.disabled = true;
  outputEl.innerHTML = '<div class="adi-loading"><div class="pulse"></div><p>AI engine analyzing: extracting core concepts → building causal chain → weighing impacts → generating paragraph…</p></div>';

  setTimeout(function() {{
    var data;
    if (ARG_DEMO_API_ISSUE) {{
      // Future: real API call
    }}
    data = MOCK_ARG_OUTPUT_ISSUE;
    renderArgDemoOutputIssue(outputEl, data);
    runBtn.disabled = false;
  }}, 1800);
}}

function renderArgDemoOutputIssue(container, data) {{
  var conceptsHtml = data.concepts.map(function(c) {{
    return '<span class="adi-concept-chip" title="' + (c.zh || '') + '">' + c.en + '</span>';
  }}).join('');

  var chainHtml = data.chain.map(function(node, i) {{
    var arrow = i < data.chain.length - 1 ? '<span class="adi-chain-arrow">→</span>' : '';
    return '<span class="adi-chain-node">' + node + '</span>' + arrow;
  }}).join('');

  var html = '';
  html += '<div class="adi-card card-concepts" style="animation: sampleFadeIn 0.4s ease both;">';
  html += '<div class="card-step">Step 1</div>';
  html += '<h4>Core Concepts · 核心概念映射</h4>';
  html += '<div class="adi-concepts">' + conceptsHtml + '</div>';
  html += '</div>';

  html += '<div class="adi-card card-chain" style="animation: sampleFadeIn 0.4s 0.1s ease both;">';
  html += '<div class="card-step">Step 2</div>';
  html += '<h4>Causal Chain · 因果链构建</h4>';
  html += '<div class="adi-chain-flow">' + chainHtml + '</div>';
  html += '</div>';

  html += '<div class="adi-card card-weighing" style="animation: sampleFadeIn 0.4s 0.2s ease both;">';
  html += '<div class="card-step">Step 3</div>';
  html += '<h4>Impact Weighing · 影响权重评估</h4>';
  html += '<div class="adi-weighing-text">' + data.weighing + '</div>';
  html += '</div>';

  html += '<div class="adi-card card-paragraph" style="animation: sampleFadeIn 0.4s 0.3s ease both;">';
  html += '<div class="card-step">Step 4</div>';
  html += '<h4>Academic Paragraph · 学术段落输出</h4>';
  html += '<div class="adi-paragraph">' + data.paragraph + '</div>';
  html += '</div>';

  container.innerHTML = html;
}}

function resetArgDemoIssue() {{
  var outputEl = document.getElementById('adi-output');
  var runBtn = document.getElementById('adi-run');
  runBtn.disabled = false;
  outputEl.innerHTML = '<div class="adi-placeholder" id="adi-placeholder"><svg viewBox="0 0 24 24" fill="none"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg><p>Click \\u300cGenerate Argument Chain\\u300d to see AI analysis</p><p class="hint">The engine will show: Core Concepts → Causal Chain → Impact Weighing → Academic Paragraph</p></div>';
}}
</script>"""

    return f'''<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{_escape_html(topic_line or "ArgueLab Training Briefing")}</title>
<style>{ISSUE_PAGE_CSS}</style>
</head>
<body>
<!-- Fixed Top Bar: Search + PDF Download + Theme Toggle -->
<div class="top-bar" id="top-bar">
  <div class="tb-search" id="tb-search">
    <span class="ts-icon">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
    </span>
    <input type="text" class="tb-search-input" id="top-search-input" placeholder="Search in page…" autocomplete="off">
    <span class="tb-search-count" id="top-search-count"></span>
    <button class="tb-search-nav" id="top-search-prev" title="Previous match" disabled>&uarr;</button>
    <button class="tb-search-nav" id="top-search-next" title="Next match" disabled>&darr;</button>
    <button class="tb-search-clear" id="top-search-clear" title="Clear search">&times;</button>
  </div>
  <a href="/issues/{issue_date}/download" class="top-action-btn" title="Download PDF" download>
    <span class="ta-icon">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
    </span>
    <span class="ta-label">PDF</span>
  </a>
  <button class="top-action-btn" id="theme-toggle-btn" aria-label="Toggle reading mode" title="Switch between Reading Mode and Dark Mode">
    <span class="ta-icon" id="toggle-icon">
      <svg id="icon-sun" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>
      <svg id="icon-moon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="display:none"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
    </span>
    <span class="ta-label" id="toggle-label">Reading Mode</span>
  </button>
</div>

<div class="issue-shell">
{toc_desktop}
<main class="issue-main">
{body}
</main>
</div>

{arg_demo_html}

<!-- Search Overlay -->
<div class="search-overlay" id="search-overlay">
  <div class="search-dialog">
    <div class="search-input-row">
      <span class="si-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      </span>
      <input type="text" class="search-input" id="search-input" placeholder="Search within this issue…" autocomplete="off">
      <span class="search-count" id="search-count"></span>
      <div class="search-nav">
        <button class="search-nav-btn" id="search-prev" title="Previous match" disabled>&uarr;</button>
        <button class="search-nav-btn" id="search-next" title="Next match" disabled>&darr;</button>
      </div>
      <button class="search-nav-btn" id="search-close" title="Close search" style="font-size:20px;line-height:1;">&times;</button>
    </div>
    <div class="search-hint-row">
      <span><kbd>Esc</kbd> to close</span>
      <span><kbd>&uarr;</kbd><kbd>&darr;</kbd> to navigate</span>
    </div>
  </div>
</div>

{toc_js}
{theme_js}
{interaction_js}
{arg_demo_js}
</body>
</html>'''


def _render_context_block(text: str) -> str:
    """Render a context sub-section block with a label and body.

    Detects bold sub-headers (**议题：**, **背景：**, **争议：**, **为什么选**, **Framing**)
    and renders them as structured label+body cards. For Framing blocks, the body is
    parsed as structured bullet points with numbered layers.
    """
    label_map = {
        "议题": ("议题", "ctx-block ctx-topic"),
        "背景": ("背景", "ctx-block ctx-bg"),
        "争议": ("争议焦点", "ctx-block ctx-debate"),
        "为什么选这个议题": ("为什么选这个议题", "ctx-block ctx-rationale"),
        "为什么选": ("为什么选这个议题", "ctx-block ctx-rationale"),
        "Framing 提示": ("Framing 提示", "ctx-block ctx-framing"),
        "Framing": ("Framing 提示", "ctx-block ctx-framing"),
    }

    text = text.strip()
    first_line = text.split("\n")[0].strip()

    # Detect which sub-header this is
    matched_key = None
    for key in label_map:
        if first_line.startswith(f"**{key}：**") or first_line.startswith(f"**{key}:**") or first_line.startswith(f"**{key}**"):
            matched_key = key
            break
        # Also match **Key** without colon followed by content
        if first_line.startswith(f"**{key}** ") or first_line.startswith(f"**{key}**\n"):
            matched_key = key
            break

    if not matched_key:
        return ""

    label, css_class = label_map[matched_key]

    # Strip the bold header from the first line
    import re
    body_lines = text.split("\n")
    first = body_lines[0]
    # Remove **Key：** or **Key:** or **Key** prefix
    first = re.sub(rf'^\*\*{re.escape(matched_key)}(?:[：:]?\*\*|\\*\\*)\s*', '', first)
    body_lines[0] = first
    body_text = "\n".join(body_lines).strip()

    if not body_text or body_text in ("：", ":", ""):
        return ""

    # For Framing blocks, parse bullet points
    if "framing" in css_class:
        bullet_items = []
        for line in body_lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith("- "):
                # Bullet item: - **（1）意图 vs 效果：** body text
                item_html = _markdown_inline_to_html(line[2:])
                bullet_items.append(f'<li>{item_html}</li>')
            else:
                # Non-bullet line — wrap as plain paragraph before list
                pass
        if bullet_items:
            return (
                f'<div class="{css_class}">'
                f'<div class="ctx-label">{label}</div>'
                f'<ul class="framing-list">{"".join(bullet_items)}</ul>'
                f'</div>'
            )
        else:
            # No bullets found — render as plain text card
            return (
                f'<div class="{css_class}">'
                f'<div class="ctx-label">{label}</div>'
                f'<div class="ctx-text">{_markdown_inline_to_html(body_text)}</div>'
                f'</div>'
            )

    # Non-framing blocks: parse bullet points if present
    has_bullets = any(line.strip().startswith("- ") for line in body_lines)
    if has_bullets:
        bullet_items = []
        for line in body_lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith("- "):
                item_html = _markdown_inline_to_html(line[2:])
                bullet_items.append(f'<li>{item_html}</li>')
        if bullet_items:
            return (
                f'<div class="{css_class}">'
                f'<div class="ctx-label">{label}</div>'
                f'<ul class="ctx-list">{"".join(bullet_items)}</ul>'
                f'</div>'
            )
    # No bullets — render as plain text card
    return (
        f'<div class="{css_class}">'
        f'<div class="ctx-label">{label}</div>'
        f'<div class="ctx-text">{_markdown_inline_to_html(body_text)}</div>'
        f'</div>'
    )


def _render_context_section(text: str) -> str:
    """Render the full context section by splitting combined text by sub-headers.

    Splits at bold sub-header boundaries (**议题：**, **背景：**, etc.),
    renders each sub-block with _render_context_block, and joins them.
    Continuation paragraphs without bold headers stay as cn-body.
    """
    if not text.strip():
        return ""

    # Split at bold sub-header boundaries (**议题：**, **背景：**, etc.)
    sub_pattern = r'\n(?=\*\*(?:议题|背景|争议|为什么选这个议题|为什么选|Framing 提示|Framing)[：:]\*\*\s)'

    blocks = re.split(sub_pattern, text)
    results = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue
        html = _render_context_block(block)
        if html:
            results.append(html)
        else:
            rendered = _markdown_inline_to_html(block)
            if rendered:
                results.append(f'<p class="cn-body ctx-continuation">{rendered}</p>')

    return "\n".join(results)


def _render_paragraph(text: str, module_type: str = "context") -> str:
    """Render a paragraph block with intelligent formatting.

    Detects special content types:
    - Context sub-headers (**议题：**, **背景：**, etc) → structured ctx-blocks
    - Passage blocks with [Thesis/Premise/etc] labels → .passage-block with color tags
    - Reading guides (starts with 📖 or similar) → .guide-block
    - Expression cards (compact labels like `揭示本质 · high register`)
    - Bullet lists (- prefix) → styled ul
    - Regular CN or EN paragraphs

    Args:
        text: The text content to render
        module_type: One of 'context', 'passage', 'expression', 'sentence', 'chain', 'output'
    """
    text = text.strip()
    if not text:
        return ""

    lines = text.split("\n")
    first_line = lines[0]

    # --- Context section (combined mode): delegate to _render_context_section ---
    if module_type == "context":
        return _render_context_section(text)

    # --- Passage block: starts with [Thesis], **Thesis**, Thesis etc (with optional > blockquote prefix) ---
    if re.match(r'^(> )?(\*\*)?\[?(Thesis|Premise|Evidence|Counter-?argument|Conclusion)\]?\s*$', first_line):
        return _render_passage_block(text)
    # Handle combined passage section text that may contain multiple blocks
    if module_type == "passage" and re.search(r'(?:^|\n)(?:> )?(?:\*\*)?\[?(Thesis|Premise|Evidence|Counter-?argument|Conclusion)', text):
        return _render_passage_section(text)

    # --- Reading guide ---
    if first_line.startswith("📖") or first_line.startswith("**📖"):
        guide_text = _markdown_inline_to_html(text)
        # Strip the emoji + bold header
        guide_text = re.sub(r'<strong>.*?📖.*?</strong>\s*', '', guide_text)
        guide_text = re.sub(r'📖\s*\*\*.*?\*\*\s*', '', guide_text)
        return f'<div class="guide-block">{guide_text}</div>'

    # --- Expression cards: ### 表达 N ---
    # When module_type is "expression", always use the section renderer
    # which handles multiple cards. Only use single-card renderer for
    # non-expression modules that happen to contain an expression card.
    if module_type == "expression":
        # Match both: ### 表达 1  and  ### 1. phrase text
        if re.search(r'^###\s*(?:(?:表达|Expression)\s*\d+|\d+\.)', text, re.MULTILINE):
            return _render_expression_section(text)
    if re.match(r'^###\s*(?:(?:表达|Expression)\s*\d+|\d+\.)', first_line):
        return _render_expression_card(text)

    # --- Sentence deconstruction: 目标句/Target Sentence / 结构拆解/Structure / 语法点/Grammar Points ---
    if ("**目标句**" in text or "**目标句：**" in text or
        "**Target Sentence" in text or
        "**结构拆解**" in text or "**结构拆解：**" in text or
        "**Structure:**" in text or
        "**语法点" in text or "**语法要点" in text or
        "**Grammar Points" in text or
        "**句型模板" in text or "**仿写练习" in text or "**适用场景" in text):
        return _render_sentence_decon(text)

    # --- Output tasks: 写作任务/Task/口语任务/Topic/Question/结构引导/自我检查 ---
    # NOTE: must check BEFORE argument chain, since output text may contain "Weighing" etc.
    if ("**写作任务" in text or "**口语任务" in text or
        "### 写作" in text or "### 口语" in text or
        "### IELTS" in text or "### Writing" in text or "### Speaking" in text or
        "**结构指引" in text or "**结构引导" in text or
        "### 结构指引" in text or "### 结构引导" in text or "### Structure Guide" in text or
        "**Self-check" in text or "**Self-Check" in text or "**自我检查" in text or
        "### 自检" in text or "### Self-Check" in text or "### Self-check" in text or
        "### Task A" in text or "### Task B" in text or "### Task 1" in text or "### Task 2" in text or
        "**Topic:**" in text or "**Question:**" in text or
        "**Structure Guide:" in text or "**Speaking Guide:" in text or
        "**题目：**" in text):
        return _render_output_tasks(text)

    # --- Argument chain: contains 中文观点 / 核心概念 / 因果链 / EN Core / Causal Chain (with or without emoji prefix) ---
    if ("**中文观点" in text or "🇨🇳" in text or
        "**核心概念**" in text or "**核心概念：**" in text or "🏗️" in text or
        "**因果链" in text or "Causal Chain" in text or "⛓️" in text or
        "**权衡" in text or "Weighing" in text or "⚖️" in text or
        "**示范段落" in text or "Sample Paragraph" in text or "Sample Argument" in text or "✍️" in text or
        "**EN Core" in text or "English Core Concept" in text):
        return _render_argument_chain(text)

    # --- Output tasks (legacy detection without Task headers, kept as fallback) ---
    # Already handled above

    # --- Source attribution line ---
    if first_line.startswith("*Source:") or first_line.startswith("*From:") or first_line.startswith("*Adapted"):
        return f'<p class="source-line">{_markdown_inline_to_html(text.strip("*"))}</p>'

    # --- Bullet list (check before italic, since - lines are not italic) ---
    if all(l.strip().startswith("- ") or l.strip() == "" for l in lines if l.strip()):
        items = []
        for l in lines:
            l = l.strip()
            if l.startswith("- "):
                items.append(f'<li>{_markdown_inline_to_html(l[2:])}</li>')
        return f'<ul>{"".join(items)}</ul>'

    # --- Italic sub-headers (only match single-line, single-asterisk) ---
    if first_line.startswith("*") and not first_line.startswith("**") and "\n" not in text.strip():
        return f'<p class="section-subtitle">{_markdown_inline_to_html(text.strip("*"))}</p>'

    # --- Horizontal rules / section separators (skip rendering) ---
    if re.match(r'^[-_*]{3,}\s*$', first_line):
        return ""

    # --- Default: Chinese or English paragraph ---
    # For long paragraphs, split into multiple <p> tags if they exceed length limits
    rendered = _markdown_inline_to_html(text)
    if any('\u4e00' <= c <= '\u9fff' for c in text):
        # Chinese: split if > 180 chars
        if len(text) > 180:
            return _split_long_paragraph(text, "cn-body")
        return f'<p class="cn-body">{rendered}</p>'
    else:
        # English: split if > 140 words
        word_count = len(text.split())
        if word_count > 140:
            return _split_long_paragraph(text, "en-body")
        return f'<p class="en-body">{rendered}</p>'


def _split_long_paragraph(text: str, css_class: str) -> str:
    """Split a long paragraph into multiple <p> tags at natural break points.

    For Chinese: splits at sentence-ending punctuation (。！？)
    For English: splits at sentence-ending punctuation (.!?)
    """
    is_cn = any('\u4e00' <= c <= '\u9fff' for c in text)

    if is_cn:
        # Split Chinese at 。！？
        parts = re.split(r'(?<=[。！？])', text)
    else:
        # Split English at . ! ?
        parts = re.split(r'(?<=[.!?])\s+', text)

    # Filter empty parts
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) <= 1:
        rendered = _markdown_inline_to_html(text)
        return f'<p class="{css_class}">{rendered}</p>'

    result = []
    for part in parts:
        rendered = _markdown_inline_to_html(part)
        result.append(f'<p class="{css_class}">{rendered}</p>')

    return "\n".join(result)


def _render_passage_section(text: str) -> str:
    """Render a combined passage section that may contain:
    - Source attribution line
    - 'Argument Structure' header
    - The actual passage block (with > prefixes and [Thesis] labels)
    - Source line
    - Reading guide
    """
    parts = []
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue

        # Source attribution
        if s.startswith("*Source:") or s.startswith("*From:") or s.startswith("*Adapted"):
            parts.append(f'<p class="source-line">{_markdown_inline_to_html(s.strip("*"))}</p>')
            i += 1
            continue

        # Source attribution in bold: **来源：** ...
        if s.startswith("**来源：**") or s.startswith("**来源**"):
            source_text = _markdown_inline_to_html(s)
            parts.append(f'<p class="source-line">{source_text}</p>')
            i += 1
            continue

        # Chinese source in blockquote: > **来源：** ...
        if s.startswith("> **来源：**") or s.startswith("> **来源**"):
            parts.append(f'<p class="source-line">{_markdown_inline_to_html(s.lstrip("> "))}</p>')
            i += 1
            continue

        # Chinese reading guide in blockquote: > **阅读指引：** ...
        if s.startswith("> **阅读指引：**") or s.startswith("> **阅读指引**") or s.startswith("> **阅读提示"):
            guide_lines = [s]
            j = i + 1
            while j < len(lines) and lines[j].strip() and not lines[j].strip().startswith("---"):
                # Stop if we encounter an arg label on its own line
                nxt_clean = lines[j].strip().lstrip("> ").strip("*")
                if re.match(r'^(?:\[)?(Thesis|Premise|Evidence|Counter-?argument|Conclusion)(?:\])?\s*$', nxt_clean):
                    break
                guide_lines.append(lines[j].strip())
                j += 1
            guide_text = "\n".join(guide_lines)
            guide_text = _markdown_inline_to_html(guide_text)
            parts.append(f'<div class="guide-block">{guide_text}</div>')
            i = j
            continue

        # "Argument Structure" / "精选段落" header — skip
        if s.startswith("**Argument Structure") or s == "**Argument Structure 标注**":
            i += 1
            continue
        if s == "**精选段落**" or s.startswith("**精选段落"):
            i += 1
            continue

        # Reading guide (emoji format) — skip heading, collect body until next section
        if s.startswith("📖") or s.startswith("**📖"):
            j = i + 1
            # skip leading blank lines after the heading
            while j < len(lines) and not lines[j].strip():
                j += 1
            guide_body_lines = []
            while j < len(lines) and lines[j].strip() and not lines[j].strip().startswith("---"):
                guide_body_lines.append(lines[j].strip())
                j += 1
            guide_text = "\n".join(guide_body_lines)
            guide_text = _markdown_inline_to_html(guide_text)
            parts.append(f'<div class="guide-block">{guide_text}</div>')
            i = j
            continue

        # Horizontal rule
        if s == "---":
            i += 1
            continue

        # Passage block: starts with > **[Thesis or > [Thesis or [Thesis (after strip)
        # Also supports **Thesis** (bold) and plain Thesis formats
        clean = s.lstrip("> ").strip("*")
        if re.match(r'^(?:\[)?(Thesis|Premise|Evidence|Counter-?argument|Conclusion)', clean):
            # Collect all passage lines until empty line or next section marker
            passage_lines = []
            j = i
            while j < len(lines):
                ls = lines[j].strip()
                if not ls:
                    break
                if ls.startswith("*Source:") or ls.startswith("*From:") or ls.startswith("📖") or ls == "---":
                    break
                passage_lines.append(ls)
                j += 1
            passage_text = "\n".join(passage_lines)
            parts.append(_render_passage_block(passage_text))
            i = j
            continue

        # Source line at end of passage
        if s.startswith("*Source:") or s.startswith("*From:"):
            parts.append(f'<p class="source-line">{_markdown_inline_to_html(s.strip("*"))}</p>')
            i += 1
            continue

        i += 1

    return "\n".join(parts) if parts else ""


def _render_passage_block(text: str) -> str:
    """Render the argument-labeled passage block.

    Supports formats:
    - **[Thesis · 论点]:** body text (bracket with label)
    - **Thesis**: body text (bold, no brackets)
    - [Thesis]: body text (plain brackets)
    """
    label_map = {
        "Thesis": "arg-thesis",
        "Premise": "arg-premise",
        "Evidence": "arg-evidence",
        "Counter-argument": "arg-counter",
        "Counterargument": "arg-counter",
        "Conclusion": "arg-conclusion",
    }
    result_parts = []
    source_line = ""
    pending_label = None  # Hold a label until we see its body on the next line

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("*Source:") or line.startswith("*From:") or line.startswith("*Adapted"):
            source_line = line.strip("*").strip()
            continue

        # Try to match as a standalone arg label: **Thesis** / [Thesis] / Thesis
        label_match = re.match(r'^(?:\*\*)?\[?(Thesis|Premise|Evidence|Counter-?argument|Conclusion)\]?(?:\*\*)?\s*$', line)
        if label_match:
            pending_label = label_match.group(1)
            continue

        # Replace inline argument labels (bracket format: [Thesis · label])
        def replace_label(m):
            label = m.group(1)
            css_class = label_map.get(label, "arg-thesis")
            return f'<span class="arg-label {css_class}">{label}</span>'
        line = re.sub(r'\[(Thesis|Premise|Evidence|Counter-argument|Counterargument|Conclusion)\s*[·•]\s*[^\]]+\]', replace_label, line)

        # Strip blockquote >
        if line.startswith("> "):
            line = line[2:]
        elif line.startswith(">"):
            line = line[1:]

        # If we have a pending label, prepend it as a styled span
        if pending_label:
            css_class = label_map.get(pending_label, "arg-thesis")
            result_parts.append(f'<span class="arg-label {css_class}">{pending_label}</span> {line}')
            pending_label = None
        else:
            result_parts.append(line)

    source_html = f'<p class="source-line">{_markdown_inline_to_html(source_line)}</p>' if source_line else ""
    body = " ".join(result_parts)
    body_html = f'<p>{_markdown_inline_to_html(body)}</p>'

    return f'<div class="passage-block">{source_html}{body_html}</div>'


def _render_expression_section(text: str) -> str:
    """Render an entire expression section that may contain:
    - Intro text (e.g. '*不是背单词...*')
    - Multiple expression cards (### 表达 N)
    """
    parts = []
    lines = text.split("\n")
    i = 0
    current_card_lines = []
    in_card = False

    while i < len(lines):
        s = lines[i].strip()

        # Detect expression card header
        if re.match(r'^###\s*(?:(?:表达|Expression)\s*\d+|\d+\.)', s):
            # Flush previous card
            if in_card and current_card_lines:
                card_text = "\n".join(current_card_lines)
                parts.append(_render_expression_card(card_text))
                current_card_lines = []
            in_card = True
            current_card_lines.append(s)
            i += 1
            continue

        # Horizontal rule - flush and skip
        if s == "---":
            if in_card and current_card_lines:
                card_text = "\n".join(current_card_lines)
                parts.append(_render_expression_card(card_text))
                current_card_lines = []
                in_card = False
            i += 1
            continue

        if in_card:
            current_card_lines.append(s)
        else:
            # Before first card — render as intro text
            if s:
                # Check if it's italic intro
                if s.startswith("*") and not s.startswith("**"):
                    parts.append(f'<p class="section-subtitle">{_markdown_inline_to_html(s.strip("*"))}</p>')
                elif s:
                    rendered = _markdown_inline_to_html(s)
                    parts.append(f'<p class="cn-body">{rendered}</p>')

        i += 1

    # Flush last card
    if in_card and current_card_lines:
        card_text = "\n".join(current_card_lines)
        parts.append(_render_expression_card(card_text))

    return "\n".join(parts)


def _render_expression_card(text: str) -> str:
    """Render a single expression card (### 表达 N / ### Expression N).

    Supports multiple briefing formats (tried in order):

    Format D (current — labeled inline fields):
    ### 表达 N — Title
    **英文表达：** `phrase`
    **功能标签：** function description
    **语域标签：** register label
    **中文释义：** CN explanation
    **常见搭配：**
    - `collocation 1`
    - `collocation 2`
    **外刊例句：** example sentence

    Format C (legacy — blockquote + bullet metadata):
    ### Expression N — Title
    > **"phrase text"**
    - **功能：** function description
    - **语域：** register label
    - **搭配链：** collocation examples
    - **例句：** example sentence

    Format A (legacy — inline code):
    ### 表达 N
    **`phrase`** `register label`
    **常见搭配：** collocations
    **例句：** example

    Format B (legacy — compact-tags):
    ### 表达 N
    **紧凑标签：**`tag1 · tag2 · tag3`
    **`phrase`** `register label`
    - **常见搭配：**...
    """
    phrase = ""
    tags = ""
    cn_explanation = ""
    collocations = ""
    example = ""
    card_num = ""

    lines = text.split("\n")
    i = 0
    current_field = None
    phrase_found = False  # first **`...`** line is phrase, second is tags

    while i < len(lines):
        s = lines[i].strip()

        # ── Strip blockquote prefix (> ) ──
        if s.startswith("> "):
            s = s[2:]

        # Extract card number from ### header
        if s.startswith("###"):
            m = re.match(r'^###\s*(?:(?:表达|Expression)\s*(\d+)|(\d+))', s)
            if m:
                card_num = m.group(1) or m.group(2)
            i += 1
            continue

        # Skip empty lines
        if not s:
            i += 1
            continue

        # ═══════════════════════════════════════════════
        # Format E (briefing current): ### N. phrase + compact inline fields
        # ### 1. phrase text
        # **功能**：desc | **语域**：label | **适用**：desc
        # **常见搭配：** collocations
        # **例句：** example
        # ═══════════════════════════════════════════════
        if re.match(r'\*\*功能[：:]', s) and not phrase:
            # This is the inline metadata line — extract phrase from the ### header
            # The phrase was stored in the card header line; extract it now
            if not phrase:
                # Phrase comes from the ### header line (stored in lines[0])
                first_content = lines[0].strip() if lines else ""
                pm = re.match(r'^###\s*\d+\.\s*(.+)', first_content)
                if pm:
                    phrase = pm.group(1).strip()
            # Parse inline tags: **功能**：desc | **语域**：label | **适用**：desc
            func_match = re.search(r'\*\*功能[：:]\*\*\s*(.+?)(?:\s*\|\s*\*\*|$)', s)
            reg_match = re.search(r'\*\*语域[：:]\*\*\s*(.+?)(?:\s*\|\s*\*\*|$)', s)
            usage_match = re.search(r'\*\*适用[：:]\*\*\s*(.+?)$', s)
            tag_parts = []
            if func_match:
                tag_parts.append(func_match.group(1).strip())
            if reg_match:
                tag_parts.append(reg_match.group(1).strip())
            if usage_match:
                tag_parts.append(usage_match.group(1).strip())
            tags = " · ".join(tag_parts) if tag_parts else ""
            current_field = "format_e"
            i += 1
            continue

        # Format E continuation: **常见搭配：** / **例句：**
        if current_field == "format_e":
            if re.match(r'\*\*常见搭配[：:]', s):
                collocations = re.sub(r'\*\*常见搭配[：:]\*\*\s*', '', s)
                current_field = "colloc"
                i += 1
                continue
            if re.match(r'\*\*例句[：:]', s):
                example = re.sub(r'\*\*例句[：:]\*\*\s*', '', s)
                current_field = "example"
                i += 1
                continue

        # ═══════════════════════════════════════════════
        # Format D (current): labeled inline fields
        # ═══════════════════════════════════════════════

        # ── **英文表达：** `phrase` ──
        if re.match(r'\*\*英文表达[：:]\*\*', s):
            phrase = re.sub(r'\*\*英文表达[：:]\*\*\s*', '', s)
            phrase = phrase.strip('`')
            phrase_found = True
            current_field = None
            i += 1
            continue

        # ── **功能标签：** text ──
        if re.match(r'\*\*功能标签[：:]\*\*', s):
            func_text = re.sub(r'\*\*功能标签[：:]\*\*\s*', '', s)
            tags = (tags + " · " + func_text) if tags else func_text
            current_field = None
            i += 1
            continue

        # ── **语域标签：** text ──
        if re.match(r'\*\*语域标签[：:]\*\*', s):
            reg_text = re.sub(r'\*\*语域标签[：:]\*\*\s*', '', s)
            tags = (tags + " | " + reg_text) if tags else reg_text
            current_field = None
            i += 1
            continue

        # ── **中文释义：** text ──
        if re.match(r'\*\*中文释义[：:]\*\*', s):
            cn_explanation = re.sub(r'\*\*中文释义[：:]\*\*\s*', '', s)
            current_field = "cn"
            i += 1
            continue

        # ── **常见搭配：** (heading, content on bullet lines) ──
        if re.match(r'\*\*常见搭配[：:]\*\*', s):
            # Check if inline content after the heading
            inline = re.sub(r'\*\*常见搭配[：:]\*\*\s*', '', s)
            if inline and not inline.startswith('-'):
                collocations = inline
            current_field = "colloc"
            i += 1
            continue

        # ── **外刊例句：** text ──
        if re.match(r'\*\*外刊例句[：:]\*\*', s):
            example = re.sub(r'\*\*外刊例句[：:]\*\*\s*', '', s)
            current_field = "example"
            i += 1
            continue

        # ── Format D bullet: - `text` in colloc mode → collocation ──
        if s.startswith("- ") and current_field == "colloc":
            content = s[2:].strip().strip('`')
            if collocations:
                collocations += "\n" + content
            else:
                collocations = content
            i += 1
            continue

        # ═══════════════════════════════════════════════
        # Legacy format detection
        # ═══════════════════════════════════════════════

        # ── Phrase: **"text"** or **`text`** ──
        if not phrase_found and not phrase:
            # Blockquote-style: **"text"** (Format C)
            mq = re.match(r'\*\*"(.+?)"\*\*$', s)
            if mq:
                phrase = mq.group(1)
                phrase_found = True
                i += 1
                continue

        # ── Field-header lines (standalone): **常见搭配：** / **例句：** ──
        if re.match(r'\*\*常见搭配[：:]', s):
            collocations = re.sub(r'\*\*常见搭配[：:]\*\*\s*', '', s)
            current_field = "colloc"
            i += 1
            continue
        if re.match(r'\*\*例句[：:]', s):
            example = re.sub(r'\*\*例句[：:]\*\*\s*', '', s)
            current_field = "example"
            i += 1
            continue

        # ── Format B: **紧凑标签：**`...` (tags line, implicit) ──
        if s.startswith("**紧凑标签：**"):
            tags = re.sub(r'\*\*紧凑标签：\*\*\s*', '', s).strip("`")
            current_field = "tags"
            phrase_found = True  # prevent subsequent phrase match from overwriting
            i += 1
            continue

        # ── Phrase or tags line: **`...`** (legacy Format A) ──
        if (s.startswith("**`") or (s.startswith("**") and "`" in s[:40])):
            if not phrase_found:
                m = re.match(r'\*\*`(.+?)`\*\*', s)
                if m:
                    phrase = m.group(1)
                else:
                    phrase = re.sub(r'\*\*', '', s)
                    phrase = re.sub(r'`[^`]*`\s*$', '', phrase).strip()
                phrase_found = True
                current_field = None
            elif not tags:
                tags = re.sub(r'\*\*', '', s).strip("`")
                current_field = "tags"
            i += 1
            continue

        # ── Bullet lines ──
        if s.startswith("- "):
            content = s[2:]

            # Format C: - **功能：** text  or  - **语域：** text → tags
            m_func = re.match(r'\*\*功能[：:]\*\*\s*(.+)', content)
            if m_func:
                func_text = m_func.group(1).strip()
                tags = (tags + " · " + func_text) if tags else func_text
                i += 1
                continue

            m_reg = re.match(r'\*\*语域[：:]\*\*\s*(.+)', content)
            if m_reg:
                reg_text = m_reg.group(1).strip()
                tags = (tags + " | " + reg_text) if tags else reg_text
                i += 1
                continue

            if re.match(r'\*\*搭配链[：:]', content):
                collocations = re.sub(r'\*\*搭配链[：:]\*\*\s*', '', content)
                current_field = "colloc"
                i += 1
                continue

            if re.match(r'\*\*常见搭配[：:]', content):
                collocations = re.sub(r'\*\*常见搭配[：:]\*\*\s*', '', content)
                current_field = "colloc"
                i += 1
                continue

            if re.match(r'\*\*例句[：:]', content):
                example = re.sub(r'\*\*例句[：:]\*\*\s*', '', content)
                current_field = "example"
                i += 1
                continue

            if not cn_explanation:
                # First non-metadata bullet → start CN explanation
                cn_explanation = content
                current_field = "cn"
            elif current_field == "cn":
                cn_explanation += "\n" + content
            elif not collocations:
                collocations = content
                current_field = "colloc"
            else:
                example = content
                current_field = "example"
            i += 1
            continue

        # ── Plain paragraph → CN explanation (legacy formats) ──
        if not cn_explanation and current_field not in ("colloc", "example", "tags"):
            cn_explanation = s
            current_field = "cn"
            i += 1
            continue

        # ── Continuation lines ──
        if s:
            if current_field == "cn":
                cn_explanation += " " + s
            elif current_field == "colloc":
                collocations += " " + s
            elif current_field == "example":
                example += " " + s
            elif current_field == "tags":
                tags += " " + s

        i += 1

    # ── Build HTML ──
    num_html = f'<div class="expr-num">Expression {card_num}</div>' if card_num else ""
    phrase_html = f'<div class="expr-phrase">{_escape_html(phrase)}</div>' if phrase else ""
    tags_html = f'<div class="expr-tags">{_escape_html(tags)}</div>' if tags else ""
    cn_html = f'<div class="expr-cn">{_markdown_inline_to_html(cn_explanation)}</div>' if cn_explanation else ""
    colloc_html = f'<div class="expr-colloc">{_markdown_inline_to_html(collocations)}</div>' if collocations else ""
    ex_html = f'<div class="expr-example">{_markdown_inline_to_html(example)}</div>' if example else ""

    return f'<div class="expr-card">{num_html}{phrase_html}{tags_html}{cn_html}{colloc_html}{ex_html}</div>'


def _render_sentence_decon(text: str) -> str:
    """Render sentence deconstruction with card-based layout.

    Handles both formats:
    1. `**目标句：** sentence` (inline)
    2. `**目标句**\n> sentence` (blockquote on next line)
    """
    parts = []
    lines = text.split("\n")

    # Extract target sentence
    target_sentence = ""
    structure_analysis = ""
    grammar_points = []
    template_text = ""
    imitation_text = ""
    scenario_text = ""

    current_mode = None  # 'target', 'structure', 'grammar', 'template', 'imitation', 'scenario'
    current_grammar_title = ""
    current_grammar_body = []

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue

        # Skip italic subtitle lines
        if s.startswith("*") and not s.startswith("**"):
            i += 1
            continue

        # Target sentence - inline format: **目标句：** / **Target Sentence:** text
        if s.startswith("**目标句：**") or s.startswith("**Target Sentence:**") or s.startswith("**Target Sentence**"):
            ts = re.sub(r'\*\*(?:目标句|Target Sentence)[：:]?\*\*\s*', '', s)
            if ts.strip():
                target_sentence = ts
            # Also consume the blockquote content on next lines (skip empty lines)
            j = i + 1
            while j < len(lines):
                ls = lines[j].strip()
                if not ls:
                    j += 1
                    continue
                if ls.startswith(">"):
                    target_sentence += " " + ls[1:].strip()
                    j += 1
                else:
                    break
            i = j
            continue

        # Target sentence - block format: **目标句** / **Target Sentence** on one line, > on next
        if (s.startswith("**目标句**") and not s.startswith("**目标句：**")) or \
           (s.startswith("**Target Sentence**") and not s.startswith("**Target Sentence:**")):
            current_mode = "target"
            i += 1
            # Consume blockquote lines (skip empty lines)
            while i < len(lines):
                ls = lines[i].strip()
                if not ls:
                    i += 1
                    continue
                if ls.startswith(">"):
                    target_sentence += " " + ls[1:].strip()
                    i += 1
                else:
                    break
            continue

        if s.startswith("**结构拆解：**") or s.startswith("**结构拆解**") or s.startswith("**结构分析：**") or s.startswith("**结构分析**") or s.startswith("**Structure:**") or s.startswith("**Structure**"):
            if current_mode == "grammar" and current_grammar_title:
                grammar_points.append((current_grammar_title, " ".join(current_grammar_body)))
                current_grammar_body = []
            # Handle both "**结构拆解：** text" and "**结构拆解**" (bare header, content on next line)
            structure_analysis = re.sub(r'\*\*(?:结构拆解|结构分析|Structure)[：:]?\*\*\s*', '', s)
            if not structure_analysis.strip():
                # Content is on next lines
                current_mode = "structure"
                i += 1
                j = i
                while j < len(lines) and lines[j].strip() and not lines[j].strip().startswith("**") and lines[j].strip() != "---":
                    ls = lines[j].strip()
                    # Strip blockquote prefix if present
                    if ls.startswith("> "):
                        ls = ls[2:]
                    structure_analysis += " " + ls
                    j += 1
                i = j
            else:
                structure_analysis = structure_analysis.strip()
                current_mode = "structure"
                i += 1
                # Also consume continuation lines
                j = i
                while j < len(lines) and lines[j].strip() and not lines[j].strip().startswith("**") and lines[j].strip() != "---":
                    ls = lines[j].strip()
                    if ls.startswith("> "):
                        ls = ls[2:]
                    structure_analysis += " " + ls
                    j += 1
                i = j
            continue

        if s.startswith("**结构模板**") or s.startswith("**结构模板：**") or s.startswith("**句型模板") or s.startswith("**模板句型"):
            if current_mode == "grammar" and current_grammar_title:
                grammar_points.append((current_grammar_title, " ".join(current_grammar_body)))
                current_grammar_body = []
            current_mode = "template"
            template_text = re.sub(r'\*\*(?:结构模板|句型模板|模板句型)[^：:]*[：:]?\*\*\s*', '', s)
            if not template_text.strip():
                # Template is on next lines (code block or blockquote)
                j = i + 1
                template_lines = []
                while j < len(lines):
                    ls = lines[j].strip()
                    if ls.startswith("```"):
                        j += 1
                        continue
                    if ls.startswith("**") or ls == "---":
                        break
                    if ls:
                        # Strip blockquote prefix
                        if ls.startswith("> "):
                            ls = ls[2:]
                        template_lines.append(ls)
                    j += 1
                template_text = "\n".join(template_lines)
                i = j
            else:
                template_text = template_text.strip("`")
                i += 1
            continue

        if s.startswith("**语法点") or s.startswith("**语法要点") or s.startswith("**Grammar Points"):
            # Check for inline title format: **语法点 N — title：** body
            # or: **语法点 N：** body
            gm = re.match(r'\*\*(?:语法点|语法要点|Grammar Points)\s*\d*\s*(?:[—–-]?\s*(.+?))[：:]?\*\*\s*(.*)', s)
            if gm and gm.group(1) and gm.group(1).strip():
                # Inline format: title and body on same line
                title = gm.group(1).strip()
                body = gm.group(2).strip()
                grammar_points.append((title, body))
                current_mode = "grammar"
                current_grammar_title = ""
                current_grammar_body = []
            else:
                # Bare header: **语法点** or **语法点 1**, content on next lines
                current_mode = "grammar"
            i += 1
            continue

        if s.startswith("**仿写模板：**") or s.startswith("**仿写模板**") or s.startswith("**模仿模板：**") or s.startswith("**模仿模板**") or s.startswith("**仿写练习：**") or s.startswith("**仿写练习**") or s.startswith("**Imitation:"):
            if current_mode == "grammar" and current_grammar_title:
                grammar_points.append((current_grammar_title, " ".join(current_grammar_body)))
                current_grammar_body = []
            current_mode = "imitation"
            # Check if content is inline (e.g. **模仿模板：** text)
            inline = re.sub(r'\*\*(?:仿写模板|模仿模板|仿写练习|Imitation)[：:]?\*\*\s*', '', s)
            if inline.strip() and not inline.strip().startswith(">"):
                imitation_text = inline.strip()
                i += 1
            else:
                i += 1
                j = i
                imitation_lines = []
                while j < len(lines):
                    ls = lines[j].strip()
                    if ls.startswith("```"):
                        j += 1
                        continue
                    if ls.startswith("**") or ls == "---":
                        break
                    # Strip blockquote prefix
                    if ls.startswith("> "):
                        ls = ls[2:]
                    elif ls.startswith(">"):
                        ls = ls[1:]
                    if ls:
                        imitation_lines.append(ls)
                    j += 1
                imitation_text = "\n".join(imitation_lines).strip("`")
                i = j
            continue

        if s.startswith("**你的仿写") or s.startswith("**仿写示例"):
            if current_mode == "grammar" and current_grammar_title:
                grammar_points.append((current_grammar_title, " ".join(current_grammar_body)))
                current_grammar_body = []
            current_mode = "imitation"
            i += 1
            j = i
            while j < len(lines) and lines[j].strip() and not lines[j].strip().startswith("**"):
                if imitation_text:
                    imitation_text += "\n"
                imitation_text += lines[j].strip().strip("*_")
                j += 1
            i = j
            continue

        if s.startswith("**仿写场景：**") or s.startswith("**仿写场景**") or s.startswith("**适用场景：**") or s.startswith("**适用场景**") or s.startswith("**场景适用：**") or s.startswith("**场景适用**"):
            if current_mode == "grammar" and current_grammar_title:
                grammar_points.append((current_grammar_title, " ".join(current_grammar_body)))
                current_grammar_body = []
            scenario_text = re.sub(r'\*\*(?:仿写场景|适用场景)[：:]?\*\*\s*', '', s)
            current_mode = "scenario"
            i += 1
            # Collect scenario items as bullet list
            j = i
            scenario_items = []
            if scenario_text.strip():
                scenario_items.append(scenario_text.strip())
            while j < len(lines):
                ls = lines[j].strip()
                if not ls:
                    j += 1
                    continue
                if ls.startswith("**") or ls.startswith("---"):
                    break
                if ls.startswith("- "):
                    scenario_items.append(ls[2:].strip())
                j += 1
            scenario_text = "\n".join(scenario_items) if scenario_items else scenario_text
            i = j
            continue

        # Code blocks (already extracted by the main parser, but may appear inline)
        if s.startswith("```"):
            i += 1
            continue

        # Grammar point lines
        if current_mode == "grammar":
            if s.startswith("- "):
                if current_grammar_title and current_grammar_body:
                    grammar_points.append((current_grammar_title, " ".join(current_grammar_body)))
                    current_grammar_body = []
                s = s[2:]
                # Match **title：** body or **title**： body or **title：** body
                m = re.match(r'\*\*(.+?)\*\*[：:]\s*(.*)', s)
                if not m:
                    # Also try **title：** (colon inside bold)
                    m = re.match(r'\*\*(.+?)[：:]\*\*\s*(.*)', s)
                if m:
                    current_grammar_title = m.group(1).strip()
                    body = m.group(2).strip()
                    if body:
                        current_grammar_body.append(body)
                else:
                    current_grammar_title = s
                    current_grammar_body = []
            else:
                current_grammar_body.append(s)
            i += 1
            continue

        # Content for structure analysis
        if current_mode == "structure":
            structure_analysis += " " + s
            i += 1
            continue

        i += 1

    # Flush last grammar point
    if current_grammar_title and current_grammar_body:
        grammar_points.append((current_grammar_title, " ".join(current_grammar_body)))

    # ── Build HTML ──
    html = ['<div class="sentence-decon">']

    # 1. Target sentence quote card
    if target_sentence:
        html.append(
            '<div class="target-sentence-card">'
            '<div class="ts-label">Target Sentence</div>'
            '<div class="ts-text">{}</div>'
            '</div>'.format(_markdown_inline_to_html(target_sentence))
        )

    # 2. Why this sentence works (structure analysis)
    if structure_analysis:
        html.append(
            '<div class="why-card">'
            '<div class="why-label">Why This Sentence Works</div>'
            '<div class="why-text">{}</div>'
            '</div>'.format(_markdown_inline_to_html(structure_analysis))
        )

    # 3. Grammar points as mini-cards
    if grammar_points:
        html.append(
            '<div class="grammar-section">'
            '<div class="grammar-section-label">Grammar Points</div>'
        )
        for g_idx, (title, body) in enumerate(grammar_points):
            html.append(
                '<div class="grammar-mini-card">'
                '<div class="gm-title">{num}. {title}</div>'
                '<div class="gm-body">{body}</div>'
                '</div>'.format(
                    num=g_idx + 1,
                    title=_escape_html(title),
                    body=_markdown_inline_to_html(body)
                )
            )
        html.append('</div>')

    # 4. Template card
    if template_text:
        html.append(
            '<div class="template-card">'
            '<div class="tpl-label">Reusable Template</div>'
            '<div class="tpl-code">{}</div>'
            '</div>'.format(_escape_html(template_text))
        )

    # 5. Imitation example
    if imitation_text:
        html.append(
            '<div class="imitation-card">'
            '<div class="imit-label">Imitation Example</div>'
            '<div class="imit-text">{}</div>'
            '</div>'.format(_markdown_inline_to_html(imitation_text))
        )

    # 6. Scenario tag
    if scenario_text:
        html.append(
            '<div class="scenario-tag">Apply to: {}</div>'.format(
                _markdown_inline_to_html(scenario_text)
            )
        )

    html.append('</div>')
    return "\n".join(html)


def _render_argument_chain(text: str) -> str:
    """Render Chinese → English argument chain with card-based layout.

    Structure:
    1. Chinese Viewpoint → chain-step
    2. English Core Concept → chain-step
    3. Causal Chain → chain-step with code
    4. Weighing → weighing-card (longer text)
    5. Sample Argument Paragraph → sample-paragraph-card
    """
    parts = ['<div class="chain-flow">']
    lines = text.split("\n")

    # Extract sections
    cn_viewpoint = ""
    core_concept = ""
    causal_chain = ""
    weighing_texts = []
    sample_para = ""
    sample_note = ""

    current_mode = None  # 'cn', 'core', 'causal', 'weighing', 'sample'

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue

        # Detect headers with various formats:
        # "**中文观点：** text", "**中文观点**", "**🇨🇳 中文观点**"
        if ("中文观点" in s[:30] and "**" in s[:30]) or s.startswith("🇨🇳"):
            cn_viewpoint = s
            # Strip all bold markers and emoji
            cn_viewpoint = re.sub(r'\*\*.*?中文观点.*?\*\*\s*', '', cn_viewpoint)
            cn_viewpoint = re.sub(r'🇨🇳\s*', '', cn_viewpoint)
            cn_viewpoint = cn_viewpoint.strip()
            current_mode = "cn"
            current_mode = "cn"
            i += 1
            # Consume continuation lines (skip blank lines)
            j = i
            while j < len(lines):
                ls = lines[j].strip()
                if not ls:
                    j += 1
                    continue
                if ls.startswith("**") or ls.startswith("🏗️") or ls.startswith("🇨🇳") or ls == "---":
                    break
                # Strip blockquote prefix if present
                if ls.startswith("> "):
                    ls = ls[2:]
                elif ls.startswith(">"):
                    ls = ls[1:]
                cn_viewpoint += " " + ls
                j += 1
            i = j
            continue

        if ("核心概念" in s[:30] and "**" in s[:30]) or s.startswith("🏗️") or "English Core Concept" in s or ("Core Concept" in s and "**" in s[:5]) or s.startswith("**EN Core**") or s.startswith("**EN Core：**") or s.startswith("**英文核心") or ("英文核心" in s[:30] and "**" in s[:30]):
            core_concept = s
            core_concept = re.sub(r'\*\*.*?核心概念.*?\*\*\s*', '', core_concept)
            core_concept = re.sub(r'\*\*.*?(?:Core Concept|English Core Concept|EN Core).*?\*\*\s*', '', core_concept)
            core_concept = re.sub(r'🏗️\s*', '', core_concept)
            core_concept = core_concept.strip()
            current_mode = "core"
            i += 1
            j = i
            while j < len(lines):
                ls = lines[j].strip()
                if not ls:
                    j += 1
                    continue
                if ls.startswith("**") or ls.startswith("⛓️") or ls == "---":
                    break
                # Strip blockquote prefix if present
                if ls.startswith("> "):
                    ls = ls[2:]
                elif ls.startswith(">"):
                    ls = ls[1:]
                core_concept += " " + ls
                j += 1
            i = j
            continue

        if ("因果链" in s[:30] and "**" in s[:30]) or s.startswith("⛓️") or "Causal Chain" in s[:30]:
            causal_chain = s
            causal_chain = re.sub(r'\*\*.*?因果链.*?\*\*\s*', '', causal_chain)
            causal_chain = re.sub(r'\*\*.*?Causal Chain.*?\*\*\s*', '', causal_chain)
            causal_chain = re.sub(r'⛓️\s*', '', causal_chain)
            causal_chain = causal_chain.strip()
            current_mode = "causal"
            i += 1
            j = i
            while j < len(lines):
                ls = lines[j].strip()
                if not ls:
                    j += 1
                    continue
                if ls.startswith("**") or ls.startswith("⚖️") or ls.startswith("✍️") or ls == "---":
                    break
                # Strip blockquote prefix if present
                if ls.startswith("> "):
                    ls = ls[2:]
                elif ls.startswith(">"):
                    ls = ls[1:]
                if not causal_chain.endswith("\n") and causal_chain:
                    causal_chain += "\n"
                causal_chain += ls
                j += 1
            i = j
            continue

        if s.startswith("**权衡：**") or s.startswith("**权衡**") or s.startswith("⚖️") or s.startswith("**⚖️") or "权衡" in s[:40] or "Weighing" in s[:40]:
            # Weighing can be multiple paragraphs
            current_mode = "weighing"
            # Skip this header line
            i += 1
            j = i
            para = []
            while j < len(lines):
                ls = lines[j].strip()
                if ls.startswith("**示范段落：**") or ls.startswith("**示范段落**") or ls.startswith("**参考段落：**") or ls.startswith("**参考段落**") or ls.startswith("✍️") or ls.startswith("**✍️") or "Sample Argument Paragraph" in ls or "Sample Paragraph" in ls[:40] or "参考段落" in ls[:30] or ls.startswith("📌") or ls.startswith("*ArgueLab") or ls == "---":
                    break
                if ls == "" and para:
                    weighing_texts.append(" ".join(para))
                    para = []
                elif ls and not ls.startswith("⚖️") and ls != "---":
                    para.append(ls)
                j += 1
            if para:
                weighing_texts.append(" ".join(para))
            i = j
            continue

        if s.startswith("**示范段落：**") or s.startswith("**示范段落**") or s.startswith("**参考段落：**") or s.startswith("**参考段落**") or s.startswith("✍️") or s.startswith("**✍️") or "Sample Argument Paragraph" in s or "Sample Paragraph" in s[:40] or "参考段落" in s[:30]:
            current_mode = "sample"
            i += 1
            j = i
            while j < len(lines):
                ls = lines[j].strip()
                if ls.startswith("📌") or ls.startswith("**📌") or ls.startswith("*ArgueLab") or ls == "---":
                    sample_note = re.sub(r'\*\*', '', ls).strip("📌").strip() if not ls.startswith("---") else ""
                    j += 1
                    break
                if ls and ls != "---":
                    sample_para += ls + " "
                j += 1
            i = j
            continue

        if s.startswith("📌") or s.startswith("**📌"):
            sample_note = re.sub(r'\*\*', '', s).strip("📌").strip()
            i += 1
            continue

        i += 1

    # Build HTML
    if cn_viewpoint:
        parts.append(
            '<div class="chain-step">'
            '<div class="step-label">Chinese Viewpoint</div>'
            '<div class="step-cn">{}</div>'
            '</div>'.format(_markdown_inline_to_html(cn_viewpoint))
        )

    if core_concept:
        parts.append(
            '<div class="chain-step">'
            '<div class="step-label">English Core Concept</div>'
            '<div class="step-en">{}</div>'
            '</div>'.format(_markdown_inline_to_html(core_concept))
        )

    if causal_chain:
        parts.append(
            '<div class="chain-step">'
            '<div class="step-label">Causal Chain</div>'
            '<div class="step-code">{}</div>'
            '</div>'.format(_escape_html(causal_chain))
        )

    if weighing_texts:
        weigh_html = ""
        for wt in weighing_texts:
            weigh_html += '<p>{}</p>'.format(_markdown_inline_to_html(wt))
        parts.append(
            '<div class="weighing-card">'
            '<div class="weigh-label">Weighing</div>'
            '<div class="weigh-text">{}</div>'
            '</div>'.format(weigh_html)
        )

    if sample_para:
        note_html = ""
        if sample_note:
            note_html = '<div class="sp-note">{}</div>'.format(_markdown_inline_to_html(sample_note))
        parts.append(
            '<div class="sample-paragraph-card">'
            '<div class="sp-label">Sample Argument Paragraph</div>'
            '<div class="sp-text">{}</div>'
            '{}'
            '</div>'.format(_markdown_inline_to_html(sample_para.strip()), note_html)
        )

    parts.append('</div>')
    return "\n".join(parts)


def _render_output_tasks(text: str) -> str:
    """Render output tasks with card-based layout.

    Supports both legacy flat format and new two-task structure:
    ### Task 1: IELTS Task 2 / TEM-8 写作
    **题目：** > prompt
    **结构引导** - step list
    **Self-Check** - checklist

    ### Task 2: IELTS Speaking Part 3 / 口语 Task
    **题目：** > prompt
    **结构引导** - step list
    **Self-Check** - checklist

    ### 参考答案提示（Premium 用户） - premium hint block
    """
    lines = text.split("\n")

    # Task-level structures: each task has {type, prompt, guide[], check[], meta}
    tasks = []
    premium_hints = []

    current_task = None
    current_mode = None  # 'prompt', 'guide', 'check', 'premium'
    current_section = None  # 'task1', 'task2', 'premium'

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue

        # Detect task headers: ### Task 1: ... / ### Task 2: ... / ### Task A: ... / ### Task B: ...
        # Also: ### 写作任务..., ### 口语训练... (current briefing format)
        # Also: ### IELTS Writing Task 2, ### IELTS Speaking Part 3, ### Writing, ### Speaking
        if (s.startswith("### Task 1") or s.startswith("### Task 2") or s.startswith("### Task A") or s.startswith("### Task B") or
            s.startswith("### 写作") or s.startswith("### 口语") or
            s.startswith("### IELTS") or s.startswith("### Writing") or s.startswith("### Speaking") or
            re.match(r'###\s*(?:Writing|Speaking|Task|口语|写作)', s)):
            if current_task:
                tasks.append(current_task)
            # Determine task type from header
            task_type = "Writing Task" if ("写作" in s or "Writing" in s) else "Speaking Task"
            current_task = {"type": task_type, "prompt": "", "guide": [], "check": [], "meta": ""}
            current_section = "task1" if "写作" in s or "Writing" in s else "task2"
            current_mode = "prompt"  # Default: next content is prompt
            # Check for meta in header (e.g. "（IELTS Task 2 / TEM-8 写作）")
            m = re.search(r'[（(]([^)）]+)[)）]', s)
            if m:
                current_task["meta"] = m.group(1)
            i += 1
            continue

        # Legacy format: **写作任务...**
        if s.startswith("**写作任务") and not tasks:
            current_task = {"type": "Writing Task", "prompt": "", "guide": [], "check": [], "meta": ""}
            prompt = re.sub(r'\*\*写作任务[（(][^)）]*[)）]\*\*\s*', '', s)
            prompt = re.sub(r'\*\*写作任务[：:]\*\*\s*', '', prompt)
            prompt = re.sub(r'\*\*写作任务[^*]*\*\*\s*', '', prompt)
            m = re.search(r'[（(]建议\s*.+[）)]', prompt)
            if m:
                current_task["meta"] = m.group(0)
                prompt = prompt.replace(m.group(0), "").strip()
            if prompt.strip():
                current_task["prompt"] = prompt.strip()
            current_section = "task1"
            current_mode = "prompt"
            i += 1
            continue

        # Legacy format: **口语任务...** or **口语训练...** or **口语表达...**
        if (s.startswith("**口语任务") or s.startswith("**口语训练") or s.startswith("**口语表达")) and not any(t["type"] == "Speaking Task" for t in tasks):
            # Flush the current (writing) task before creating the speaking task
            if current_task:
                tasks.append(current_task)
                current_task = None
            current_task = {"type": "Speaking Task", "prompt": "", "guide": [], "check": [], "meta": ""}
            prompt = re.sub(r'\*\*口语任务[（(][^)）]*[)）]\*\*\s*', '', s)
            prompt = re.sub(r'\*\*口语任务[：:]\*\*\s*', '', prompt)
            prompt = re.sub(r'\*\*口语任务[^*]*\*\*\s*', '', prompt)
            m = re.search(r'[（(]建议\s*.+[）)]', prompt)
            if m:
                current_task["meta"] = m.group(0)
                prompt = prompt.replace(m.group(0), "").strip()
            if prompt.strip():
                current_task["prompt"] = prompt.strip()
            current_mode = "prompt"
            i += 1
            continue

        # Premium section
        if s.startswith("### 参考答案提示") or s.startswith("### 参考") or "Premium" in s[:30]:
            if current_task:
                tasks.append(current_task)
                current_task = None
            current_section = "premium"
            current_mode = "premium"
            i += 1
            continue

        # Sub-headers within a task
        if s.startswith("**题目：**") or s.startswith("**题目**") or s.startswith("**Topic:**") or s.startswith("**Topic**") or s.startswith("**Question:**") or s.startswith("**Question**"):
            current_mode = "prompt"
            # Check if prompt text is inline
            inline = re.sub(r'\*\*(?:题目|Topic|Question)[：:]?\*\*\s*', '', s)
            if inline.strip():
                current_task["prompt"] = inline.strip()
            i += 1
            continue

        if s.startswith("**结构引导：**") or s.startswith("**结构引导**") or s.startswith("**结构指引") or s.startswith("**Structure Guide") or s.startswith("**Speaking Guide") or s.startswith("**结构指南") or s.startswith("**思维拓展"):
            current_mode = "guide"
            i += 1
            continue
        # Shared guide via ### header (e.g. ### 结构指引, ### Structure Guide)
        if s.startswith("### 结构引导") or s.startswith("### 结构指引") or s.startswith("### 结构指南") or s.startswith("### Structure Guide") or s.startswith("### Speaking Guide") or s.startswith("### 思维拓展"):
            current_mode = "guide"
            i += 1
            continue

        if s.startswith("**Self-Check") or s.startswith("**Self-check") or s.startswith("**自我检查") or s.startswith("**自测清单"):
            current_mode = "check"
            i += 1
            continue
        # Shared check via ### header (e.g. ### 自检清单, ### Self-Check)
        if s.startswith("### 自检") or s.startswith("### Self-Check") or s.startswith("### Self-check") or s.startswith("### 自我检查") or s.startswith("### 自测"):
            current_mode = "check"
            i += 1
            continue

        # Content collection based on current mode
        if current_mode == "prompt" and current_task:
            if s.startswith("> "):
                if current_task["prompt"]:
                    current_task["prompt"] += " "
                current_task["prompt"] += s[2:].strip()
            i += 1
            continue

        if current_mode == "guide" and current_task:
            if s.startswith("- "):
                current_task["guide"].append(s[2:])
            elif re.match(r'^\d+[.\)]\s', s):
                # Numbered items: 1. ..., 2) ...
                current_task["guide"].append(re.sub(r'^\d+[.\)]\s*', '', s))
            i += 1
            continue

        if current_mode == "check" and current_task:
            if s.startswith("- [ ]") or s.startswith("- [x]"):
                current_task["check"].append(s[5:].strip() if s.startswith("- [ ]") else s[5:].strip())
            elif s.startswith("- "):
                current_task["check"].append(s[2:])
            i += 1
            continue

        if current_mode == "premium":
            if s and not s.startswith("---"):
                premium_hints.append(s)
            i += 1
            continue

        i += 1

    # Flush last task
    if current_task:
        tasks.append(current_task)

    # Post-processing: if tasks share guide/check (e.g. only the last task
    # captured them), propagate to earlier tasks that are missing them.
    if len(tasks) > 1:
        last = tasks[-1]
        for t in tasks[:-1]:
            if not t["guide"] and last["guide"]:
                t["guide"] = list(last["guide"])
            if not t["check"] and last["check"]:
                t["check"] = list(last["check"])

    # Build HTML
    html = []

    # Render each task as a separate block
    for task in tasks:
        task_html = []
        task_html.append('<div class="task-block">')

        # Task header with type badge
        meta_html = ' <span class="task-meta">{}</span>'.format(_escape_html(task["meta"])) if task["meta"] else ""
        task_html.append(
            '<div class="task-header">'
            '<span class="task-type">{}</span>'
            '{}'
            '</div>'.format(_escape_html(task["type"]), meta_html)
        )

        # Task prompt
        if task["prompt"]:
            task_html.append(
                '<div class="task-prompt">{}</div>'.format(_markdown_inline_to_html(task["prompt"]))
            )

        # Structure guide
        if task["guide"]:
            items_html = ""
            for item in task["guide"]:
                items_html += '<li>{}</li>'.format(_markdown_inline_to_html(item))
            task_html.append(
                '<div class="guide-card">'
                '<div class="guide-label">Structure Guide</div>'
                '<ol class="step-list">{}</ol>'
                '</div>'.format(items_html)
            )

        # Self-check
        if task["check"]:
            items_html = ""
            for item in task["check"]:
                items_html += '<li>{}</li>'.format(_markdown_inline_to_html(item))
            task_html.append(
                '<div class="check-card">'
                '<div class="check-label">Self-Check</div>'
                '<ul class="checklist">{}</ul>'
                '</div>'.format(items_html)
            )

        task_html.append('</div>')
        html.extend(task_html)

    # Premium hints block
    if premium_hints:
        hints_html = "<br>".join(_markdown_inline_to_html(h) for h in premium_hints)
        html.append(
            '<div class="premium-hint-card">'
            '<div class="ph-label">Premium 参考答案提示</div>'
            '<div class="ph-text">{}</div>'
            '</div>'.format(hints_html)
        )

    if not html:
        # Fallback: render as plain paragraphs (no recursion)
        paras = []
        for line in text.split('\n'):
            line = line.strip()
            if line:
                paras.append(f'<p>{_markdown_inline_to_html(line)}</p>')
        return '\n'.join(paras)

    return "\n".join(html)


# ── Internal Search Engine ──

class BriefingSearchEngine:
    """Full-text search across all ArgueLab briefing content.
    
    Indexes every briefing .md file, parsing content into typed sections
    (context, passage, expressions, sentence, argument_chain, output).
    """
    
    SECTION_KEYWORDS = {
        "今日议题背景": "context",
        "外刊核心段落": "passage",
        "5个可迁移表达": "expressions",
        "高级句型拆解": "sentence",
        "中文观点": "argument_chain",
        "输出任务": "output",
        "来源附录": "source_notes",
    }
    
    def __init__(self, briefing_dir: Path):
        self.briefing_dir = briefing_dir
        self.docs = []     # [{date, topic, source, url, sections: [{type, heading, content}]}]
        self._build()
    
    def _parse(self, filepath: Path) -> dict | None:
        text = filepath.read_text(encoding="utf-8")
        lines = text.split("\n")
        doc = {"date": "", "topic": "", "source": "", "url": "", "sections": []}
        
        # Parse frontmatter (YAML `---` blocks)
        in_fm = False
        has_fm = False
        for line in lines:
            if line.strip() == "---":
                if not in_fm:
                    in_fm = True
                    has_fm = True
                    continue
                else:
                    break
            if in_fm:
                if line.startswith("date:"):
                    doc["date"] = line.split(":", 1)[1].strip()
                elif line.startswith("topic:"):
                    doc["topic"] = line.split(":", 1)[1].strip().strip('"')
                elif line.startswith("source:"):
                    doc["source"] = line.split(":", 1)[1].strip().strip('"')
                elif line.startswith("url:"):
                    doc["url"] = line.split(":", 1)[1].strip().strip('"')
        
        # Fallback: parse legacy format (no YAML frontmatter)
        if not has_fm or not doc["date"]:
            for line in lines:
                # Extract date from heading: "# ArgueLab — 今日训练简报 | June 25, 2026"
                if line.startswith("# ") and ("ArgueLab" in line or "简报" in line):
                    m = re.search(r'(\d{4}-\d{2}-\d{2})', line)
                    if m:
                        doc["date"] = m.group(1)
                    else:
                        # Try "June 25, 2026" format → YYYY-MM-DD
                        from datetime import datetime as _dt
                        m2 = re.search(r'([A-Z][a-z]+ \d{1,2},? \d{4})', line)
                        if m2:
                            try:
                                d = _dt.strptime(m2.group(1).replace(",", ""), "%B %d %Y")
                                doc["date"] = d.strftime("%Y-%m-%d")
                            except ValueError:
                                pass
                # Extract topic from "> **今日议题：** ..." or "**议题：** ..."
                if "议题" in line and "**" in line:
                    topic = re.sub(r'^>\s*\*+\s*今日议题[：:]\s*\*+', '', line)
                    topic = re.sub(r'\*+', '', topic).strip()
                    if topic and not doc["topic"]:
                        doc["topic"] = topic
        
        if not doc["date"]:
            return None
        
        # Parse sections
        current_section = None
        section_lines = []
        
        for line in lines:
            if line.startswith("## "):
                if current_section and section_lines:
                    content = "\n".join(section_lines).strip()
                    if content:
                        doc["sections"].append({
                            "type": current_section["type"],
                            "heading": current_section["heading"],
                            "content": content
                        })
                
                heading = line[3:].strip()
                stype = "other"
                for kw, t in self.SECTION_KEYWORDS.items():
                    if kw in heading:
                        stype = t
                        break
                current_section = {"type": stype, "heading": heading}
                section_lines = []
            elif current_section:
                section_lines.append(line)
        
        # Last section
        if current_section and section_lines:
            content = "\n".join(section_lines).strip()
            if content:
                doc["sections"].append({
                    "type": current_section["type"],
                    "heading": current_section["heading"],
                    "content": content
                })
        
        return doc
    
    def _build(self):
        self.docs = []
        if not self.briefing_dir.exists():
            return
        for mdfile in sorted(self.briefing_dir.glob("*.md")):
            doc = self._parse(mdfile)
            if doc:
                self.docs.append(doc)
    
    def search(self, query: str, limit: int = 15) -> list:
        """Return ranked search results across all indexed content."""
        q = query.strip().lower()
        if not q:
            return []
        terms = q.split()
        hits = []
        
        for di, doc in enumerate(self.docs):
            # Topic match (high-weight)
            if q in doc["topic"].lower():
                hits.append({
                    "date": doc["date"], "topic": doc["topic"],
                    "section_type": "topic", "section_heading": "议题",
                    "snippet": doc["topic"], "score": 5.0,
                    "url": f"/issues/{doc['date']}"
                })
            
            for sec in doc["sections"]:
                content_lower = sec["content"].lower()
                if q not in content_lower:
                    continue
                
                score = sum(content_lower.count(t) for t in terms)
                # Section-type boosts
                boost = {"expressions": 1.5, "sentence": 1.3, "passage": 1.2,
                         "argument_chain": 1.1}.get(sec["type"], 1.0)
                score *= boost
                
                # Snippet
                idx = content_lower.find(q)
                start = max(0, idx - 50)
                end = min(len(sec["content"]), idx + len(query) + 100)
                snip = sec["content"][start:end].strip()
                if start > 0:
                    snip = "…" + snip
                if end < len(sec["content"]):
                    snip += "…"
                
                hits.append({
                    "date": doc["date"], "topic": doc["topic"],
                    "section_type": sec["type"],
                    "section_heading": sec["heading"],
                    "snippet": snip, "score": score,
                    "url": f"/issues/{doc['date']}"
                })
        
        hits.sort(key=lambda r: r["score"], reverse=True)
        return hits[:limit]
    
    def stats(self) -> dict:
        return {
            "total_docs": len(self.docs),
            "total_sections": sum(len(d["sections"]) for d in self.docs)
        }


# ============================================================
#  InternalSearchEngine — Two-tier search for ArgueLab
#  Phase 1: Full-text search (implemented)
#  Phase 2: Semantic / embedding search (reserved stub)
# ============================================================

class InternalSearchEngine:
    """Two-tier internal knowledge search for ArgueLab.
    
    Phase 1 (implemented):
        Full-text / LIKE search across knowledge units extracted from
        briefing files: keywords, expressions, sentence patterns,
        argument chains, issue/section titles, and full section content.

    Phase 2 (reserved):
        Semantic search via embeddings (cosine similarity).
        Embedding generation / storage stubbed out.
    
    searchMode: "text" | "semantic" | "hybrid"  (default "text")
    """
    
    # ── Section-to-type map (identical to BriefingSearchEngine) ──
    SECTION_KEYWORDS = {
        "今日议题背景": "context",
        "外刊核心段落": "passage",
        "5个可迁移表达": "expressions",
        "高级句型拆解": "sentence",
        "中文观点": "argument_chain",
        "输出任务": "output",
        "来源附录": "source_notes",
    }

    # ── Field-level search weights (higher = more relevant) ──
    WEIGHTS = {
        "issue_title":   10.0,   # Exact match on issue topic is top-relevance
        "keyword":        5.0,   # Backtick expression keywords
        "expression":     4.0,   # Named expression patterns
        "collocation":    3.5,   # Common collocations
        "sentence_pattern": 3.5, # Template / target sentence patterns
        "argument_chain": 3.0,   # Causal chain / core argument
        "section_heading": 2.0,  # Section heading text
        "section_content": 1.0,  # Raw section body (with type boost below)
    }

    # ── Per-section-type boost multiplier for raw content hits ──
    SECTION_BOOST = {
        "expressions": 1.5,
        "sentence":    1.3,
        "passage":     1.2,
        "argument_chain": 1.1,
    }

    # ── Supported search modes ──
    VALID_MODES = {"text", "semantic", "hybrid"}

    # ============================================================
    #  Constructor & Index Builder
    # ============================================================

    def __init__(self, briefing_dir: Path):
        self.briefing_dir = briefing_dir
        # docs: original parsed documents (backward compat format)
        self.docs: list[dict] = []
        # knowledge_units: typed, weightable index entries
        # Each entry: {type, date, topic, text, heading, context_snippet, source_issue}
        self.knowledge_units: list[dict] = []
        # Phase 2 (reserved): embedding cache
        self._embeddings_loaded = False
        self._embedding_cache: dict = {}  # unit_hash → embedding vector
        self._build()

    # ============================================================
    #  Phase 1: Knowledge Extraction from .md files
    # ============================================================

    @staticmethod
    def _extract_keywords(expression_content: str) -> list[str]:
        """Extract backtick-coded expression keywords from expression sections.
        
        Example input:  "**英文表达：** `would not have [past participle] had it not been for`"
        Example output: ["would not have [past participle] had it not been for"]
        """
        cats = []
        for match in re.finditer(r'`([^`]+)`', expression_content):
            phrase = match.group(1).strip()
            # Skip template markers like [past participle] in the key, but keep
            # the phrase; if it's very short or entirely a placeholder, skip
            if len(phrase) >= 3 and not re.match(r'^\[.+\]$', phrase):
                cats.append(phrase)
        return cats

    @staticmethod
    def _extract_expressions(section_content: str) -> list[dict]:
        """Extract structured expression entries from expressions sections.
        
        Returns list of {phrase, cn_meaning, collocations, example, function_tag}.
        Handles the  ### 表达 N — <title>  sub-section format.
        """
        results = []
        # Split by ### 表达 N — blocks
        blocks = re.split(r'\n###\s*表达\s*\d+', section_content)
        # blocks[0] is text before first expression sub-header — skip
        for block in blocks[1:]:
            entry = {}
            # 英文表达
            m = re.search(r'\*\*英文表达[：:]\*\*\s*`([^`]+)`', block)
            if not m:
                m = re.search(r'\*\*英文表达[：:]\*\*\s*(.+?)(?:\n|$)', block)
            if m:
                entry["phrase"] = m.group(1).strip().strip('`')
            # 中文释义
            m = re.search(r'\*\*中文释义[：:]\*\*\s*(.+?)(?:\n|$)', block)
            if m:
                entry["cn_meaning"] = m.group(1).strip()
            # 功能标签
            m = re.search(r'\*\*功能标签[：:]\*\*\s*(.+?)(?:\n|$)', block)
            if m:
                entry["function_tag"] = m.group(1).strip()
            # 常见搭配
            collocs = []
            for cm in re.finditer(r'`([^`]+)`', block):
                c = cm.group(1).strip()
                if len(c) >= 5:
                    collocs.append(c)
            entry["collocations"] = collocs
            # 外刊例句
            m = re.search(r'\*\*外刊例句[：:]\*\*\s*\*?(.+?)(?:\n|$)', block)
            if m:
                entry["example"] = m.group(1).strip().strip('*')
            if entry.get("phrase"):
                results.append(entry)
        return results

    @staticmethod
    def _extract_sentence_patterns(section_content: str) -> list[dict]:
        """Extract sentence anatomy entries from sentence sections.
        
        Returns list of {target, pattern, structure}.
        """
        results = []
        entry = {}
        # 目标句
        m = re.search(r'\*\*目标句[：:]\*\*\s*\n?\s*>\s*\*?(.+?)(?:\n\s*\n|\n\*\*)', section_content, re.DOTALL)
        if m:
            entry["target"] = m.group(1).strip().strip('*')
        # 模板句型
        m = re.search(r'\*\*模板句型[：:]\*\*\s*\n?\s*```\s*(.+?)```', section_content, re.DOTALL)
        if not m:
            m = re.search(r'\*\*模板句型[：:]\*\*\s*\n?(.+?)(?:\n\*\*|\n\n)', section_content, re.DOTALL)
        if m:
            entry["pattern"] = m.group(1).strip()
        # 结构分析 (first sentence)
        m = re.search(r'\*\*结构分析[：:]\*\*\s*\n?(.+?)(?:\n|。)', section_content)
        if m:
            entry["structure"] = m.group(1).strip()
        if entry:
            results.append(entry)
        return results

    @staticmethod
    def _extract_argument_chain(section_content: str) -> list[dict]:
        """Extract argument chain entries.
        
        Returns list of {cn_viewpoint, en_core, chain_steps}.
        """
        results = []
        entry = {}
        # 中文观点
        m = re.search(r'\*\*中文观点[：:]\*\*\s*(.+?)(?:\n\n|\n\*\*)', section_content, re.DOTALL)
        if m:
            entry["cn_viewpoint"] = m.group(1).strip()
        # 英文核心句
        m = re.search(r'\*\*英文核心句[：:]\*\*\s*(.+?)(?:\n\n|\n\*\*)', section_content, re.DOTALL)
        if m:
            entry["en_core"] = m.group(1).strip()
        # 因果链
        m = re.search(r'\*\*因果链展开[：:]\*\*\s*\n(.+?)(?:\n\n|\n\*\*)', section_content, re.DOTALL)
        if m:
            steps = []
            for line in m.group(1).strip().split('\n'):
                line = re.sub(r'^\d+\.\s*', '', line).strip()
                if line:
                    steps.append(line)
            entry["chain_steps"] = steps
        if entry:
            results.append(entry)
        return results

    # ============================================================
    #  Index Builder
    # ============================================================

    _EMBEDDING_DIR_NAME = ".search_embeddings"

    def _build(self):
        """Parse all briefing .md files and build knowledge-unit index."""
        self.docs = []
        self.knowledge_units = []

        if not self.briefing_dir.exists():
            return

        for mdfile in sorted(self.briefing_dir.glob("*.md")):
            from datetime import datetime as _dt
            text = mdfile.read_text(encoding="utf-8")
            lines = text.split("\n")

            # ── Parse frontmatter ──
            date, topic, source, url = "", "", "", ""
            in_fm = False
            has_fm = False
            for line in lines:
                if line.strip() == "---":
                    if not in_fm:
                        in_fm = True; has_fm = True; continue
                    else:
                        break
                if in_fm:
                    if line.startswith("date:"):
                        date = line.split(":", 1)[1].strip()
                    elif line.startswith("topic:"):
                        topic = line.split(":", 1)[1].strip().strip('"')
                    elif line.startswith("source:"):
                        source = line.split(":", 1)[1].strip().strip('"')
                    elif line.startswith("url:"):
                        url = line.split(":", 1)[1].strip().strip('"')

            if not date:
                continue

            # ── Parse sections ──
            current_type = None
            current_heading = ""
            current_lines = []
            sections = []

            for line in lines:
                if line.startswith("## "):
                    if current_type and current_lines:
                        content = "\n".join(current_lines).strip()
                        if content:
                            sections.append({
                                "type": current_type,
                                "heading": current_heading,
                                "content": content,
                            })
                    heading = line[3:].strip()
                    current_type = "other"
                    for kw, t in self.SECTION_KEYWORDS.items():
                        if kw in heading:
                            current_type = t; break
                    current_heading = heading
                    current_lines = []
                elif current_type:
                    current_lines.append(line)

            if current_type and current_lines:
                content = "\n".join(current_lines).strip()
                if content:
                    sections.append({
                        "type": current_type,
                        "heading": current_heading,
                        "content": content,
                    })

            doc = {"date": date, "topic": topic, "source": source, "url": url, "sections": sections}
            self.docs.append(doc)

            # ── Build knowledge units ──

            # 1) Issue title
            if topic:
                self.knowledge_units.append({
                    "type": "issue_title", "date": date, "topic": topic,
                    "text": topic, "heading": "议题",
                    "context_snippet": topic,
                    "source_issue": date,
                })

            for sec in sections:
                stype = sec["type"]
                content = sec["content"]

                # 2) Section heading
                self.knowledge_units.append({
                    "type": "section_heading", "date": date, "topic": topic,
                    "text": sec["heading"], "heading": sec["heading"],
                    "context_snippet": sec["heading"],
                    "source_issue": date,
                })

                # 3) Keywords (backtick phrases from expression sections)
                if stype == "expressions":
                    for kw in self._extract_keywords(content):
                        self.knowledge_units.append({
                            "type": "keyword", "date": date, "topic": topic,
                            "text": kw, "heading": sec["heading"],
                            "context_snippet": f"Expression keyword: {kw}",
                            "source_issue": date,
                        })

                # 4) Structured expressions
                if stype == "expressions":
                    for expr in self._extract_expressions(content):
                        phrase = expr.get("phrase", "")
                        if phrase:
                            self.knowledge_units.append({
                                "type": "expression", "date": date, "topic": topic,
                                "text": phrase, "heading": sec["heading"],
                                "context_snippet": f"{phrase} — {expr.get('cn_meaning', '')}",
                                "source_issue": date,
                                "_extra": {
                                    "cn_meaning": expr.get("cn_meaning", ""),
                                    "function_tag": expr.get("function_tag", ""),
                                }
                            })
                        for coll in expr.get("collocations", []):
                            self.knowledge_units.append({
                                "type": "collocation", "date": date, "topic": topic,
                                "text": coll, "heading": sec["heading"],
                                "context_snippet": f"Collocation: {coll} (from {phrase})",
                                "source_issue": date,
                            })

                # 5) Sentence patterns (from sentence anatomy)
                if stype == "sentence":
                    for pat in self._extract_sentence_patterns(content):
                        for key in ("target", "pattern"):
                            text = pat.get(key, "")
                            if text:
                                self.knowledge_units.append({
                                    "type": "sentence_pattern", "date": date,
                                    "topic": topic, "text": text,
                                    "heading": sec["heading"],
                                    "context_snippet": text[:150] + ("…" if len(text) > 150 else ""),
                                    "source_issue": date,
                                })

                # 6) Argument chains
                if stype == "argument_chain":
                    for chain in self._extract_argument_chain(content):
                        for field, label in [("cn_viewpoint", "CN"), ("en_core", "EN")]:
                            text = chain.get(field, "")
                            if text:
                                self.knowledge_units.append({
                                    "type": "argument_chain", "date": date,
                                    "topic": topic, "text": text,
                                    "heading": f"{label}核心：{text[:30]}…",
                                    "context_snippet": text[:150] + ("…" if len(text) > 150 else ""),
                                    "source_issue": date,
                                })

                # 7) Section content (raw — fallback for broad queries)
                # Strip markdown formatting for cleaner snippets
                clean_content = re.sub(r'\*{1,3}', '', content)
                clean_content = re.sub(r'`', '', clean_content)
                clean_content = re.sub(r'>\s*', '', clean_content)
                self.knowledge_units.append({
                    "type": "section_content", "date": date, "topic": topic,
                    "text": clean_content, "heading": sec["heading"],
                    "context_snippet": clean_content[:200] + ("…" if len(clean_content) > 200 else ""),
                    "source_issue": date,
                    "_section_type": stype,  # for type-specific boosting
                })

        # ── Phase 2: try loading persisted embeddings (non-blocking) ──
        self._load_embeddings()

    # ============================================================
    #  Phase 1: Full-text Search (searchMode="text")
    # ============================================================

    def _search_text(self, query: str, limit: int = 15) -> list[dict]:
        """Full-text / LIKE search across all knowledge unit fields.
        
        Scoring:
            For each knowledge unit, compute:
              score = weight × term_frequency × (1 + partial_bonus)
            where partial_bonus = 0.5 if the full query appears
            anywhere in the unit but NOT as a standalone term.
            
        Multi-term queries: individual term scores are summed.
        Results are deduplicated by (date, text) and sorted descending.
        """
        q = query.strip().lower()
        if not q:
            return []

        terms = q.split()
        scored = []  # list of (dedup_key, score_sum, hit_dict)

        for unit in self.knowledge_units:
            text_lower = unit["text"].lower()
            heading_lower = unit.get("heading", "").lower()
            snippet_lower = unit.get("context_snippet", "").lower()
            combined = text_lower + " " + heading_lower + " " + snippet_lower

            score = 0.0

            # --- Exact query match bonus ---
            if q in combined:
                score += 1.0

            # --- Per-term matching ---
            for t in terms:
                count = text_lower.count(t)
                if count > 0:
                    score += count

            if score == 0.0:
                continue

            # --- Apply field weight ---
            weight = self.WEIGHTS.get(unit["type"], 1.0)
            score *= weight

            # --- Section-type boost for raw content ---
            if unit["type"] == "section_content":
                st = unit.get("_section_type", "other")
                boost = self.SECTION_BOOST.get(st, 1.0)
                score *= boost

            # --- Dedup key: (date, text) ---
            dedup_key = (unit["date"], unit["text"][:80])

            # --- Build snippet ---
            snippet = unit.get("context_snippet", unit["text"][:200])
            # Highlight query in snippet
            q_lower = q
            idx = snippet.lower().find(q_lower)
            if idx >= 0:
                start = max(0, idx - 30)
                end = min(len(snippet), idx + len(q) + 100)
                snippet = snippet[start:end]
                if start > 0:
                    snippet = "…" + snippet
                if end < len(unit.get("context_snippet", "")):
                    snippet += "…"

            hit = {
                "date": unit["date"],
                "topic": unit.get("topic", ""),
                "section_type": unit["type"],
                "section_heading": unit.get("heading", ""),
                "snippet": snippet,
                "score": round(score, 2),
                "url": f"/issues/{unit['date']}",
            }

            scored.append((dedup_key, score, hit))

        # --- Deduplicate: keep highest score per (date, text) ---
        seen = {}
        for key, s, hit in scored:
            if key not in seen or s > seen[key][0]:
                seen[key] = (s, hit)

        hits = [v[1] for v in seen.values()]
        hits.sort(key=lambda r: r["score"], reverse=True)
        return hits[:limit]

    # ============================================================
    #  Phase 2: Semantic Search (reserved stub)
    # ============================================================

    def _embedding_dir(self) -> Path:
        """Persistent directory for embedding cache files."""
        d = self.briefing_dir.parent / self._EMBEDDING_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_embeddings(self):
        """Load persisted embeddings from disk (stub — no-op in Phase 1)."""
        # Reserved for Phase 2: load .npy or .json embedding cache
        edir = self._embedding_dir()
        cache_file = edir / "embeddings.json"
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    self._embedding_cache = json.load(f)
                self._embeddings_loaded = True
            except Exception:
                self._embedding_cache = {}

    def _save_embeddings(self):
        """Persist embedding cache to disk (stub — no-op in Phase 1)."""
        edir = self._embedding_dir()
        cache_file = edir / "embeddings.json"
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(self._embedding_cache, f, ensure_ascii=False)
        except Exception:
            pass  # non-critical

    def _build_embeddings(self, force: bool = False):
        """Generate embeddings for all knowledge units (reserved stub).
        
        Phase 2 implementation plan:
          1. For each knowledge unit, call embedding model (e.g. text2vec / OpenAI)
          2. Store {unit_hash: embedding_vector} in self._embedding_cache
          3. Persist to .search_embeddings/embeddings.json
        """
        if self._embeddings_loaded and not force:
            return
        # TODO — Phase 2: implement actual embedding generation
        # Pseudocode:
        #   from sentence_transformers import SentenceTransformer
        #   model = SentenceTransformer('all-MiniLM-L6-v2')
        #   for unit in self.knowledge_units:
        #       key = hashlib.md5(unit["text"].encode()).hexdigest()
        #       if key not in self._embedding_cache:
        #           self._embedding_cache[key] = model.encode(unit["text"]).tolist()
        #   self._save_embeddings()
        self._embeddings_loaded = True

    def _search_semantic(self, query: str, limit: int = 15) -> list[dict]:
        """Semantic / embedding similarity search (reserved stub).
        
        Phase 2 implementation plan:
          1. Encode query → embedding vector
          2. Compute cosine similarity against all cached unit embeddings
          3. Return top-k results sorted by similarity
        """
        # Stub: return empty with a notice
        return [{
            "date": "",
            "topic": "",
            "section_type": "notice",
            "section_heading": "语义检索尚未开放",
            "snippet": (
                "Semantic (embedding-based) search is planned for Phase 2. "
                "Currently only full-text search (searchMode=text) is available. "
                "Switch to text mode to get results."
            ),
            "score": 0.0,
            "url": "",
        }]

    def _search_hybrid(self, query: str, limit: int = 15) -> list[dict]:
        """Hybrid search combining text + semantic (reserved stub).
        
        Phase 2 implementation plan:
          1. Run _search_text() and _search_semantic() in parallel
          2. Normalize scores from each to [0, 1]
          3. Combine with configurable weight: 0.7 * text + 0.3 * semantic (default)
          4. Re-rank and return top-k
        """
        # Fallback: text-only until semantic is implemented
        return self._search_text(query, limit=limit)

    # ============================================================
    #  Public API
    # ============================================================

    def search(self, query: str, limit: int = 15,
               search_mode: str = "text") -> list[dict]:
        """Main search entry point. Dispatch by searchMode.
        
        Args:
            query: Search query string.
            limit: Max results to return.
            search_mode: "text" | "semantic" | "hybrid"
        
        Returns:
            List of hit dicts with keys:
            {date, topic, section_type, section_heading, snippet, score, url}
        """
        mode = search_mode.strip().lower()
        if mode not in self.VALID_MODES:
            mode = "text"

        if mode == "semantic":
            return self._search_semantic(query, limit=limit)
        elif mode == "hybrid":
            return self._search_hybrid(query, limit=limit)
        else:
            return self._search_text(query, limit=limit)

    def stats(self) -> dict:
        return {
            "total_docs": len(self.docs),
            "total_sections": sum(len(d["sections"]) for d in self.docs),
            "knowledge_units": len(self.knowledge_units),
            "embeddings_loaded": self._embeddings_loaded,
            "embedding_cache_size": len(self._embedding_cache),
            "available_modes": ["text", "semantic (stub)", "hybrid (stub → text)"],
        }

# (initialized lazily — uses InternalSearchEngine for rich two-tier search)
_search_engine: InternalSearchEngine | None = None

def _get_search_engine() -> InternalSearchEngine:
    global _search_engine
    if _search_engine is None:
        _search_engine = InternalSearchEngine(_get_briefing_dir())
    return _search_engine


# ── Web Search: DuckDuckGo Instant Answer API + search URL fallback ──

def _web_search(query: str, limit: int = 10) -> list:
    """Search the web via DuckDuckGo Instant Answer API.
    
    Returns structured results: [{title, snippet, url, type}]
    Types: 'abstract' (DDG abstract/summary), 'related' (related topic),
           'external_link' (open in DuckDuckGo).
    """
    results = []
    
    # 1. Try DuckDuckGo Instant Answer API for definitions/abstracts
    import urllib.parse, json as _json
    try:
        encoded = urllib.parse.quote(query)
        api_url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(api_url, headers={"User-Agent": "ArgueLab/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        
        # Abstract / definition
        abstract = (data.get("AbstractText") or "").strip()
        abstract_url = data.get("AbstractURL") or ""
        if abstract and len(abstract) > 10:
            results.append({
                "title": data.get("Heading", query) or query,
                "snippet": abstract[:500],
                "url": abstract_url or f"https://duckduckgo.com/?q={encoded}",
                "type": "abstract"
            })
        
        # Related topics
        for topic in data.get("RelatedTopics", [])[:limit - len(results)]:
            if isinstance(topic, dict) and "Text" in topic:
                text = topic["Text"]
                url = topic.get("FirstURL", "")
                results.append({
                    "title": re.sub(r' - .*$', '', text)[:120],
                    "snippet": re.sub(r'<[^>]+>', '', text)[:300],
                    "url": url,
                    "type": "related"
                })
    except Exception:
        pass
    
    # 2. Always provide a "full search" link as fallback
    encoded = urllib.parse.quote(query)
    results.append({
        "title": f"Open full web search for \"{query}\"",
        "snippet": f"Click to open DuckDuckGo search results for \"{query}\" in a new tab.",
        "url": f"https://duckduckgo.com/?q={encoded}",
        "type": "external_link"
    })
    
    return results[:limit]


def _web_search_post(query: str, context: dict | None = None, limit: int = 10) -> dict:
    """POST /api/search/web — richer response with source labels, summary, disclaimer.

    Returns {query, results, summary, disclaimer}.
    Gracefully degrades when web search is unavailable.
    """
    disclaimer = "Web results are retrieved from public sources and should be independently verified before citation."

    if not WEB_SEARCH_ENABLED:
        return {
            "query": query,
            "results": [],
            "summary": "Web search is not currently configured on this server. Use in-site search or Explain instead.",
            "disclaimer": disclaimer
        }

    raw_results = _web_search(query, limit=limit)

    # Map result types to human-readable source labels
    _SOURCE_MAP = {
        "abstract": "DuckDuckGo Abstract",
        "related": "DuckDuckGo Related",
        "external_link": "DuckDuckGo Search",
    }

    results = []
    for r in raw_results:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "source": _SOURCE_MAP.get(r.get("type", ""), "Web Search"),
            "snippet": r.get("snippet", ""),
            "publishedAt": None,  # DDG free API doesn't provide publish dates
        })

    # Generate a brief neutral summary
    def _generate_summary(results: list, query: str) -> str:
        count = len(results)
        if count == 0:
            return f"No publicly searchable results found for \"{query}\"."
        # Count by type for a more informative summary
        n_abstracts = sum(1 for r in results if r["source"] == "DuckDuckGo Abstract")
        n_related = sum(1 for r in results if r["source"] == "DuckDuckGo Related")
        n_external = sum(1 for r in results if r["source"] == "DuckDuckGo Search")
        parts = []
        if n_abstracts:
            parts.append(f"{n_abstracts} definitional/abstract result{'s' if n_abstracts > 1 else ''}")
        if n_related:
            parts.append(f"{n_related} related topic{'s' if n_related > 1 else ''}")
        if n_external:
            parts.append("an external search link")
        joined = ", ".join(parts)
        last_result = results[0]
        return f"Found {count} result{'s' if count > 1 else ''} for \"{query}\" from public web sources ({joined}). The top result is \"{last_result['title'][:80]}\". These results are aggregated from the DuckDuckGo API and should not be treated as authoritative — always verify with primary sources."

    summary = _generate_summary(results, query)

    return {
        "query": query,
        "results": results,
        "summary": summary,
        "disclaimer": disclaimer
    }


# ── Expression Lookup / Explain Engine ──

# Built-in phrase dictionary for common academic / argument expressions
_EXPLAIN_DB = {
    # ── Cause & effect ──
    "treat symptoms rather than causes": {
        "plainMeaning": "只处理表面问题，而没有解决根本原因。",
        "argumentFunction": "Used to criticize a policy or approach as superficial — addressing visible outcomes without tackling root causes.",
        "chineseExplanation": "这个表达适合用来批评某个政策或措施看似有所反应，但实际上没有触及问题的根源。在议论文中，它可以用来指出某项政策是 'short-term fix' 而非 'structural solution'。",
        "reusablePatterns": [
            "This policy treats symptoms rather than causes.",
            "The proposal may address the visible problem, but it leaves the underlying cause untouched.",
            "Focusing on [X] risks treating symptoms rather than causes."
        ],
        "example": "A social media ban may reduce screen time, but it risks treating symptoms rather than causes — the real issue is algorithmic design, not device access."
    },
    "the true cause of": {
        "plainMeaning": "……的真正原因（用于引出比表面原因更深层的结构性解释）。",
        "argumentFunction": "Used to redirect the reader from superficial explanations to deeper structural or systemic causes — a key move in analytical writing.",
        "chineseExplanation": "在议论文中用于超越表面现象、引出深层结构性原因的论证动作。后面通常接一个抽象名词（neglect, inequality, design）而非具体事件。",
        "reusablePatterns": [
            "The true cause of the crisis is not X but Y.",
            "We must ask what the true cause of the disparity is.",
            "Corruption, not the earthquake, was the true cause of the death toll."
        ],
        "example": "Decades of economic collapse and institutional neglect — rather than the earthquake itself — are the true cause of the staggering death toll."
    },
    "systemic failure": {
        "plainMeaning": "系统性失败（指整个体系或制度在设计或执行上的全面失灵）。",
        "argumentFunction": "Used to elevate criticism from individual errors to structural/institutional flaws — a powerful conclusion-building term.",
        "chineseExplanation": "在政策评论、学术写作中用来指出问题不仅仅是某个人的失误，而是整个系统在设计或执行层面的失败。这个词暗示需要的是制度性改革而非修补。",
        "reusablePatterns": [
            "This reveals a systemic failure in disaster preparedness.",
            "The crisis has exposed a systemic failure of governance.",
            "What we are witnessing is not an isolated incident but a systemic failure."
        ],
        "example": "The slow rescue effort has exposed a systemic failure of disaster preparedness that no amount of international aid can disguise."
    },
    "not a matter of X but of Y": {
        "plainMeaning": "不是X的问题，而是Y的问题（用于重新定义议题的核心）。",
        "argumentFunction": "Used to reframe an issue by rejecting a common misconception and redirecting attention to a deeper or more relevant factor.",
        "chineseExplanation": "议论文中最有力的框架重置工具之一。将一个议题从人们通常认为的层面（如'talent/地理/自然'）重新定位到另一个更应被关注的层面（如'effort/治理/政策'）。",
        "reusablePatterns": [
            "The gap is not a matter of [uncontrollable factor] but of [controllable factor].",
            "Success in this field is not a matter of innate talent but of deliberate practice.",
            "The crisis is not a matter of resource scarcity but of resource distribution."
        ],
        "example": "The gap between the scale of the disaster and the capacity of the response is not a matter of geography but of governance."
    },
    "on paper": {
        "plainMeaning": "在纸面上（指法律、政策、规定等名义上存在，但实际未执行）。",
        "argumentFunction": "Used to highlight the gap between formal rules and actual practice — a classic move in policy critique.",
        "chineseExplanation": "用于指出某项制度名义上虽然存在，但现实中并未得到执行。暗示的是 'de jure vs. de facto' 或 'rhetoric vs. reality' 之间的差距。",
        "reusablePatterns": [
            "The law exists on paper but is not enforced.",
            "On paper, the regulations are stringent.",
            "The policy looks good on paper but fails in practice."
        ],
        "example": "Venezuela's building safety regulations exist on paper but have been systematically under-enforced for decades."
    },
    "against all odds": {
        "plainMeaning": "尽管万般困难，出乎意料地（形容在极端不利条件下取得的成就或生存）。",
        "argumentFunction": "Used to highlight individual resilience against systemic failure — often carries an implicit critique of the system that created the 'odds'.",
        "chineseExplanation": "常用于新闻报道或叙事写作中，描写在极度不利条件下仍然取得的成功。在议论文中使用时要注意：它可以被用来赞美个体的坚韧，也可以被用来反衬系统的不作为。",
        "reusablePatterns": [
            "survive against all odds",
            "succeed against all odds",
            "be rescued against all odds after 100+ hours"
        ],
        "example": "Aarón Levi Cantillo Vargas survived against all odds after 106 hours — a miracle that also lays bare how few others had the same chance."
    },
    "would not have ... had it not been for": {
        "plainMeaning": "如果不是因为……，就不会……（反事实条件句的高级紧缩形式）。",
        "argumentFunction": "Used to construct a counterfactual argument — showing how an outcome could have been different under different conditions. A hallmark of advanced academic writing.",
        "chineseExplanation": "反事实条件句是学术写作中非常重要的论证工具。它将读者的注意力从'已经发生的事情'引向'本可以避免的事情'，从而建立问责或提出替代方案。注意这个结构相当于 'if it had not been for X, Y would not have happened' 的倒装紧缩。",
        "reusablePatterns": [
            "The buildings would not have collapsed had it not been for weak enforcement.",
            "She would not have survived had it not been for the rapid response team.",
            "The crisis would not have escalated had it not been for the delayed international response."
        ],
        "example": "The death toll would not have been so staggering had it not been for decades of neglect in enforcing seismic building codes."
    },
    "outpace": {
        "plainMeaning": "超过，赶超（速度或规模上超过另一事物）。",
        "argumentFunction": "Used to emphasize that one trend is growing faster than another — creating a sense of urgency or imbalance. Common in data-driven argumentation.",
        "chineseExplanation": "不仅是'超过'的意思，而是强调某个事物在以更快的速度增长、以至于产生了危险的差距或失衡。在公共政策、公共卫生、经济学话题中非常高频。",
        "reusablePatterns": [
            "demand outpaces supply",
            "infection rates outpace the response capacity",
            "The problem is that [X] is outpacing [Y]."
        ],
        "example": "The current outbreak has outpaced the early trajectory of the 2014 West Africa crisis, raising alarms among global health officials."
    },
    "unaccounted for": {
        "plainMeaning": "下落不明，无法解释（指人或物未被记录或找到）。",
        "argumentFunction": "Used to flag a data gap or transparency issue — often deployed to question the adequacy of a response or the reliability of official figures.",
        "chineseExplanation": "用于公共危机报道和政策分析中，指出某个群体或资源没有被纳入官方统计或管理范围。这是从'已知的可知'转向'未知的未知'的修辞动作。",
        "reusablePatterns": [
            "remain unaccounted for",
            "funds remain unaccounted for",
            "nearly 300 people are unaccounted for"
        ],
        "example": "Nearly 300 people who tested positive for Ebola are unaccounted for, raising fears of undetected community transmission."
    },
    "dangerously underfunded": {
        "plainMeaning": "资金严重不足到产生危险的程度。",
        "argumentFunction": "Used to criticize the gap between declared commitment and actual resource allocation — a sharp way to call out political rhetoric vs. material reality.",
        "chineseExplanation": "不仅是'资金不足'，而是资金短缺已经造成了切实的危险后果。加入 'dangerously' 这个副词是在暗示：这不是一般的预算问题，而是正在危及生命的政治失职。",
        "reusablePatterns": [
            "a dangerously underfunded public health system",
            "remain dangerously underfunded",
            "The response is critically and dangerously underfunded."
        ],
        "example": "Only 13% of the pledged $910m has been delivered — leaving the Ebola response dangerously underfunded."
    },
    "obstructed by": {
        "plainMeaning": "被……阻碍/阻挠（指受到障碍物、冲突或反对的阻碍）。",
        "argumentFunction": "Used to explain why a theoretically sound plan failed in practice — typically pointing to external constraints beyond the actor's control.",
        "chineseExplanation": "用于分析某项工作未能完成的原因，强调外部障碍（conflict/bureaucracy/opposition）而非内部能力不足。在政策分析中，这是一种为执行者保留面子的批评策略。",
        "reusablePatterns": [
            "obstructed by conflict",
            "obstructed by bureaucratic delays",
            "severely obstructed by a lack of political will"
        ],
        "example": "Contact tracing efforts are obstructed by the ongoing armed conflict, which prevents health workers from accessing displacement camps."
    },
    "at the same stage": {
        "plainMeaning": "在同一阶段（用于与历史事件进行同期对比）。",
        "argumentFunction": "Used to establish a historical baseline for comparison — enables 'like-for-like' analysis by controlling for the time variable.",
        "chineseExplanation": "学术分析和新闻评论中常用的比较框架。不是笼统地比较两个事件，而是将两者在'同一发展阶段'的数据/情况进行精确对比，增强了论证的严谨性。",
        "reusablePatterns": [
            "at the same stage of the outbreak",
            "at the same stage last year",
            "The two crises are comparable at the same stage."
        ],
        "example": "At the same stage, the 2014 West Africa outbreak had only 239 cases — this time, the number exceeds 1,100."
    },
    "lay bare": {
        "plainMeaning": "揭露，暴露（指让原本隐藏的、不愿被看到的事物暴露出来）。",
        "argumentFunction": "Used to describe how a crisis or event reveals underlying truths that were previously ignored or concealed. Strong rhetorical force.",
        "chineseExplanation": "比 'reveal' 或 'expose' 更有力量——暗含的不是中性的'展现'，而是将某些人想要隐瞒的、丑陋的真相强行暴露在公众视野中。这是批评性写作的利器。",
        "reusablePatterns": [
            "The crisis has laid bare the inadequacy of the current system.",
            "The report lays bare years of institutional neglect.",
            "This incident lays bare a deeper structural problem."
        ],
        "example": "The earthquake has laid bare decades of negligence in enforcing seismic building codes."
    },
    "at the door of": {
        "plainMeaning": "归咎于某人/某机构（把责任放在……的门口）。",
        "argumentFunction": "Used to assign responsibility or blame to a specific entity — a direct and unambiguous way to establish accountability.",
        "chineseExplanation": "这是一个带有强烈问责意味的表达方式。它不只是说'X导致了Y'，而是说'责任在X身上'——这个隐喻将抽象的'问责'转化为具象的画面：把问题放到某人/某机构的门口。",
        "reusablePatterns": [
            "lay the blame at the door of the government",
            "The responsibility lies at the door of the international community.",
            "We cannot lay all the blame at the door of one institution."
        ],
        "example": "It is tempting to lay the blame entirely at the door of the Venezuelan state."
    },
    "a matter of": {
        "plainMeaning": "一个……的问题（用于定义某个议题的本质属性）。",
        "argumentFunction": "Used to categorize or define the nature of a problem — frames the discussion by asserting what the core issue 'really is'.",
        "chineseExplanation": "议论文中非常高频的框架设定短语。通过 'not a matter of X but of Y' 的结构，作者可以重新定义议题的本质。注意这个短语后面接名词，不接从句。",
        "reusablePatterns": [
            "This is a matter of principle, not convenience.",
            "The delay was a matter of logistics, not politics.",
            "Climate adaptation is increasingly a matter of survival."
        ],
        "example": "Whether the international community intervenes is a matter of both legal obligation and moral responsibility."
    },
    "rather than": {
        "plainMeaning": "而不是（表示选择或对比）。",
        "argumentFunction": "Used to contrast two alternatives — often to reject a common assumption and promote a preferred interpretation or course of action.",
        "chineseExplanation": "议论文写作的基础对比连接词。和 'instead of' 的区别在于：'rather than' 更常用于抽象对比（概念 vs. 概念），而 'instead of' 更偏向具体替换（行动 vs. 行动）。在学术写作中 'rather than' 更受偏好，因为它不暗示其中一个选择绝对错误。",
        "reusablePatterns": [
            "X rather than Y",
            "focus on prevention rather than treatment",
            "adaptation rather than mitigation"
        ],
        "example": "We should focus on prevention rather than spending resources on post-disaster reconstruction."
    },
    "the gap between": {
        "plainMeaning": "……之间的差距（用于指出两个事物之间的显著差异或不匹配）。",
        "argumentFunction": "Used to identify a discrepancy or mismatch — often the starting point of a critical analysis. What follows is typically an explanation of why this gap exists.",
        "chineseExplanation": "这是分析性写作中最常用的'提出问题'句式。通过在开篇指出一个 gap/discrepancy/divide，作者为后续的分析（解释这个差距为何存在、如何弥合）建立了清晰的论证方向。",
        "reusablePatterns": [
            "The gap between rich and poor is widening.",
            "There is a widening gap between policy and practice.",
            "The gap between promise and delivery has become unbridgeable."
        ],
        "example": "The gap between the scale of the disaster and the capacity of the response reveals a systemic governance failure."
    },
}

def _lookup_explain(selected_text: str, context: dict = None) -> dict:
    """Generate a learning-oriented explanation for a selected phrase.

    First checks the built-in phrase dictionary, then falls back to a
    template-based explanation generated from the text's characteristics.
    """
    text = selected_text.strip()
    text_lower = text.lower()

    # Exact match
    if text_lower in _EXPLAIN_DB:
        entry = _EXPLAIN_DB[text_lower]
        return {
            "selectedText": text,
            "plainMeaning": entry["plainMeaning"],
            "argumentFunction": entry["argumentFunction"],
            "chineseExplanation": entry["chineseExplanation"],
            "reusablePatterns": entry["reusablePatterns"],
            "example": entry["example"],
            "source": "built-in"
        }

    # Fuzzy match: check if any key is contained in the text
    best_match = None
    best_len = 0
    for key in _EXPLAIN_DB:
        if key in text_lower and len(key) > best_len:
            best_match = key
            best_len = len(key)

    if best_match:
        entry = _EXPLAIN_DB[best_match]
        return {
            "selectedText": text,
            "plainMeaning": entry["plainMeaning"],
            "argumentFunction": entry["argumentFunction"],
            "chineseExplanation": entry["chineseExplanation"],
            "reusablePatterns": entry["reusablePatterns"],
            "example": entry["example"],
            "source": "fuzzy-match"
        }

    # Fallback: template-based explanation
    return _generate_template_explanation(text, context)


def _generate_template_explanation(text: str, context: dict = None) -> dict:
    """Generate a template-based explanation when no exact match exists."""
    words = text.split()
    word_count = len(words)

    # Determine likely function based on text characteristics
    text_lower = text.lower()

    if word_count <= 2:
        # Single word or short phrase
        if any(w in text_lower for w in ["not", "never", "no"]):
            func = "Used for negation or contrast — often to dismiss a common assumption before presenting an alternative view."
        elif any(w in text_lower for w in ["should", "must", "need to"]):
            func = "Used to express obligation or recommendation — common in prescriptive/policy-oriented argumentation."
        elif any(w in text_lower for w in ["because", "since", "due to"]):
            func = "Used to introduce causation — explains why something happens or happened."
        elif any(w in text_lower for w in ["however", "but", "yet", "although"]):
            func = "Used to introduce contrast or concession — acknowledges an opposing view before rebutting it."
        elif any(w in text_lower for w in ["therefore", "thus", "consequently", "as a result"]):
            func = "Used to introduce a logical conclusion — signals that what follows is derived from preceding arguments."
        else:
            func = "A key term or phrase in this passage — understanding it is essential for grasping the author's argument."
    elif any(marker in text_lower for marker in ["not a matter of", "rather than", "instead of"]):
        func = "Used to reframe an issue by rejecting one interpretation and redirecting attention to another — a core argumentative move."
    elif any(marker in text_lower for marker in ["the reason", "due to", "because", "explains why"]):
        func = "Used to establish causation — links an outcome to its underlying cause(s)."
    elif any(marker in text_lower for marker in ["however", "nevertheless", "on the other hand", "by contrast"]):
        func = "Used to introduce a counter-argument or contrasting perspective — essential for balanced academic writing."
    elif any(marker in text_lower for marker in ["for example", "for instance", "such as", "specifically"]):
        func = "Used to provide concrete evidence or illustration — strengthens an abstract claim with specific detail."
    else:
        func = "An expression worth mastering — understanding its rhetorical function helps you use it effectively in your own academic writing."

    return {
        "selectedText": text,
        "plainMeaning": f"This expression ({word_count} word{'s' if word_count > 1 else ''}) conveys a specific meaning in academic/argumentative context. Its interpretation depends on the surrounding context.",
        "argumentFunction": func,
        "chineseExplanation": f"这个表达在学术论证语境中有特定的修辞功能。请在上下文中体会其用法——注意作者为什么选择这个词/短语而不是同义替代词。建议将这个表达与你在本期的可迁移表达卡片中积累的词汇一起学习。",
        "reusablePatterns": [
            f"... {text} ...",
            f"The author uses '{text}' to indicate that ..."
        ],
        "example": f"Consider how '{text}' is used in the passage — try writing your own sentence using this expression in a similar argumentative context.",
        "source": "template"
    }


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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._send_static("index.html", "text/html; charset=utf-8")
        elif self.path.startswith("/issues/"):
            self._serve_issue_page()
        elif self.path == "/api/subscribers":
            subs = load_subscribers()
            self._send_json({"count": len(subs), "subscribers": [{k: s[k] for k in ("email","name","subscribed_at")} for s in subs]})
        elif self.path == "/api/health":
            self._send_json({"status": "ok", "subscribers": len(load_subscribers()), "email_configured": bool(RESEND_API_KEY or SMTP_HOST)})
        elif self.path == "/api/debug":
            # Debug endpoint: check Supabase connectivity
            import os as _os
            info = {
                "supabase_available": SUPABASE_AVAILABLE,
                "supabase_import_error": SUPABASE_IMPORT_ERROR,
                "supabase_url_set": bool(_os.environ.get("SUPABASE_URL")),
                "supabase_key_set": bool(_os.environ.get("SUPABASE_SERVICE_KEY")),
                "subscribers_count": len(load_subscribers()),
                "email_configured": bool(RESEND_API_KEY or SMTP_HOST),
                "smtp_host": SMTP_HOST or "(not set)",
            }
            # Try a live Supabase query
            if SUPABASE_AVAILABLE:
                try:
                    from supabase_client import get_subscribers
                    sb_subs = get_subscribers()
                    info["supabase_query"] = f"ok ({len(sb_subs)} rows)"
                except Exception as e:
                    info["supabase_query"] = f"error: {e}"
            self._send_json(info)
        elif self.path == "/api/preview":
            # Show latest briefing preview
            briefing_dir = _get_briefing_dir()
            if briefing_dir.exists():
                files = sorted(briefing_dir.glob("*.md"), reverse=True)
                if files:
                    md = files[0].read_text(encoding="utf-8")
                    _, html, _ = build_email_html(md, issue_number=1, read_url="#", pdf_url="#", recipient_name="Preview")
                    self._send_json({"html": html})
                    return
            self._send_json({"error": "No briefing found"}, 404)
        elif self.path.startswith("/api/search/internal"):
            # Internal knowledge search with two-tier mode support
            #   ?q=<query>&limit=<N>&searchMode=text|semantic|hybrid
            import urllib.parse
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            query = params.get("q", [""])[0].strip()
            if not query:
                self._send_json({"error": "Missing query parameter 'q'"}, 400)
                return
            try:
                limit = min(int(params.get("limit", ["15"])[0]), 50)
            except ValueError:
                limit = 15
            search_mode = params.get("searchMode", ["text"])[0].strip().lower()
            if search_mode not in ("text", "semantic", "hybrid"):
                search_mode = "text"
            try:
                engine = _get_search_engine()
                results = engine.search(query, limit=limit, search_mode=search_mode)
                self._send_json({
                    "query": query,
                    "total": len(results),
                    "source": "arguelab_internal",
                    "searchMode": search_mode,
                    "stats": engine.stats(),
                    "results": results
                })
            except Exception as e:
                self._send_json({"error": "Internal search unavailable", "detail": str(e)}, 500)
        elif self.path.startswith("/api/search/web"):
            # Web search proxy via DuckDuckGo HTML
            import urllib.parse
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            query = params.get("q", [""])[0].strip()
            if not query:
                self._send_json({"error": "Missing query parameter 'q'"}, 400)
                return
            try:
                limit = min(int(params.get("limit", ["10"])[0]), 20)
            except ValueError:
                limit = 10
            results = _web_search(query, limit=limit)
            self._send_json({
                "query": query,
                "total": len(results),
                "source": "web",
                "results": results
            })
        elif self.path == "/api/config":
            self._send_json({
                "web_search_enabled": WEB_SEARCH_ENABLED,
                "explain_enabled": True,
            })
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def _serve_issue_page(self):
        """Serve a self-hosted issue page from briefing markdown.
        
        URL pattern: /issues/YYYY-MM-DD  or  /issues/YYYY-MM-DD/download
        
        Reads the briefing .md file, renders it as a beautiful dark-themed
        HTML page using the same design system as the main arguelab-backend.
        """
        path = self.path
        # Strip /issues/ prefix
        slug = path[len("/issues/"):]
        
        # Handle /download suffix
        is_pdf = False
        if slug.endswith("/download"):
            slug = slug[:-len("/download")]
            is_pdf = True
        
        # Extract date from slug (YYYY-MM-DD)
        date_match = re.match(r'^(\d{4}-\d{2}-\d{2})$', slug)
        if not date_match:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found - use /issues/YYYY-MM-DD")
            return
        
        issue_date = date_match.group(1)
        
        # Find briefing file
        briefing_dir = _get_briefing_dir()
        briefing_file = briefing_dir / f"{issue_date}-briefing.md"
        
        md_text = None
        
        # Try loading from .md file first
        if briefing_file.exists():
            md_text = briefing_file.read_text(encoding="utf-8")
        
        # Fallback: load from issues.json (populated by ingest_briefing.py)
        if not md_text:
            issues = load_issues()
            issue = next((i for i in issues if i.get("slug") == issue_date), None)
            if issue and "content_json" in issue:
                raw_md = issue["content_json"].get("raw_markdown", "")
                if raw_md:
                    md_text = raw_md
        
        if not md_text:
            # Try listing what's available
            available = sorted(briefing_dir.glob("*.md")) if briefing_dir.exists() else []
            available_dates = [f.stem.replace("-briefing", "") for f in available]
            self.send_response(404)
            self.end_headers()
            msg = f"Issue not found for {issue_date}. Available: {', '.join(available_dates[:5])}"
            self.wfile.write(msg.encode("utf-8"))
            return

        # If we loaded from issues.json (not from .md file), write to fallback dir
        if not briefing_file.exists():
            fallback_dir = BASE_DIR / "briefings"
            fallback_dir.mkdir(exist_ok=True)
            briefing_file = fallback_dir / f"{issue_date}-briefing.md"
            briefing_file.write_text(md_text, encoding="utf-8")

        # PDF download — generate PDF on the fly
        if is_pdf:
            self._serve_pdf(briefing_file, issue_date)
            return

        # Render issue page
        try:
            html = _render_issue_page(md_text, issue_date)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Error rendering issue: {e}".encode("utf-8"))

    def _serve_pdf(self, briefing_file: Path, issue_date: str):
        """Generate and serve a PDF for the issue.

        Calls the Node render-pdf.js script (which uses Puppeteer).
        Caches the generated PDF in a local 'pdf' directory to avoid
        re-generating on every request.
        """
        import subprocess

        # Check cache first
        pdf_dir = BASE_DIR / "pdf"
        pdf_dir.mkdir(exist_ok=True)
        pdf_path = pdf_dir / f"{issue_date}.pdf"

        if pdf_path.exists():
            # Serve cached PDF
            body = pdf_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f'inline; filename="arguelab-{issue_date}.pdf"')
            self.end_headers()
            self.wfile.write(body)
            return

        # Find Node executable
        node_exe = os.environ.get("NODE_PATH", "")
        if not node_exe or not Path(node_exe).exists():
            for candidate in ["/usr/local/bin/node", "/opt/homebrew/bin/node",
                             "/Users/hbigdog/.nvm/versions/node/v24.16.0/bin/node"]:
                if Path(candidate).exists():
                    node_exe = candidate
                    break
            else:
                node_exe = "node"  # hope it's in PATH

        # Find Chromium/Chrome executable
        chromium_path = os.environ.get("CHROMIUM_PATH", "")
        if not chromium_path or not Path(chromium_path).exists():
            for candidate in [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
            ]:
                if Path(candidate).exists():
                    chromium_path = candidate
                    break
            else:
                chromium_path = "/usr/bin/chromium"  # fallback, render-pdf.js has its own default too

        script_path = BASE_DIR / "scripts" / "render-pdf.js"
        if not script_path.exists():
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"PDF renderer script not found. Install Node + Puppeteer.")
            return

        # Build env with CHROMIUM_PATH
        render_env = os.environ.copy()
        render_env["CHROMIUM_PATH"] = chromium_path

        # Generate PDF
        try:
            result = subprocess.run(
                [node_exe, str(script_path), str(briefing_file), str(pdf_path)],
                capture_output=True, text=True, timeout=60, env=render_env
            )
            if result.returncode != 0:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"PDF generation failed: {result.stderr}".encode("utf-8"))
                return
        except subprocess.TimeoutExpired:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"PDF generation timed out (60s).")
            return
        except FileNotFoundError:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"Node.js not found. Install Node.js and set NODE_PATH.")
            return

        if not pdf_path.exists():
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"PDF file was not created.")
            return

        # Serve the generated PDF
        body = pdf_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'inline; filename="arguelab-{issue_date}.pdf"')
        self.end_headers()
        self.wfile.write(body)


    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b"{}"
        content_type = self.headers.get("Content-Type", "")

        # Handle both JSON and URL-encoded form data
        if "application/json" in content_type:
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {}
        else:
            data = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}

        if self.path == "/api/subscribe":
            email = data.get("email", "").strip()
            if not email or "@" not in email:
                self._send_json({"status": "error", "message": "Valid email required."}, 400)
                return
            name = data.get("name", "")
            result = add_subscriber(email, name)
            self._send_json(result)

        elif self.path == "/api/issues":
            content_json = data.get("content_json")
            if not content_json:
                self._send_json({"error": "Missing content_json"}, 400)
                return
            slug = content_json.get("header", {}).get("slug")
            if not slug:
                self._send_json({"error": "Missing slug in content_json.header"}, 400)
                return
            status_val = data.get("status", "draft")
            issue_data = {
                "content_json": content_json,
                "status": status_val,
            }
            result = upsert_issue(slug, issue_data)
            self._send_json(result, 201)

        elif self.path == "/api/send-to":
            to_email = data.get("to", "").strip()
            subject = data.get("subject", "ArgueLab Briefing")
            html_body = data.get("html", "")
            if not to_email or "@" not in to_email:
                self._send_json({"error": "Valid 'to' email required"}, 400)
                return
            if not html_body:
                self._send_json({"error": "'html' body required"}, 400)
                return
            try:
                send_email(to_email, subject, html_body)
                self._send_json({"status": "sent", "to": to_email})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif self.path == "/api/lookup/explain":
            selected_text = data.get("selectedText", "").strip()
            if not selected_text:
                self._send_json({"error": "Missing selectedText"}, 400)
                return
            context = data.get("context", {})
            try:
                result = _lookup_explain(selected_text, context)
                self._send_json(result)
            except Exception as e:
                self._send_json({"error": "Explain lookup failed", "detail": str(e)}, 500)

        elif self.path == "/api/search/web":
            query = data.get("query", "").strip()
            if not query:
                self._send_json({"error": "Missing query"}, 400)
                return
            try:
                context = data.get("context", {})
                limit_raw = context.get("limit", 10) if isinstance(context, dict) else 10
                limit = min(int(limit_raw), 20)
            except (ValueError, TypeError):
                limit = 10
            try:
                result = _web_search_post(query, context=context, limit=limit)
                self._send_json(result)
            except Exception as e:
                self._send_json({"error": "Web search unavailable", "detail": str(e)}, 500)

        else:
            self._send_json({"error": "Not found"}, 404)

    def do_PUT(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b"{}"
        content_type = self.headers.get("Content-Type", "")

        if "application/json" in content_type:
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return
        else:
            self._send_json({"error": "Unsupported Content-Type"}, 415)
            return

        # Update issue status
        match = re.match(r"^/api/issues/(.+)$", self.path)
        if match:
            slug = match.group(1)
            existing = next((i for i in load_issues() if i.get("slug") == slug), None)
            if not existing:
                self._send_json({"error": "Issue not found"}, 404)
                return
            status_val = data.get("status", existing.get("status", "draft"))
            updated = upsert_issue(slug, {"status": status_val})
            self._send_json(updated, 200)

        # Sync subscribers (receive merged list from local machine)
        elif self.path == "/api/subscribers/sync":
            subs_data = data.get("subscribers", [])
            if not isinstance(subs_data, list):
                self._send_json({"error": "subscribers must be an array"}, 400)
                return
            save_subscribers(subs_data)
            self._send_json({"status": "ok", "count": len(subs_data)})

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
    parser = argparse.ArgumentParser(description="ArgueLab — subscription backend")
    parser.add_argument("--send", metavar="PATH", help="Send briefing markdown to all subscribers")
    parser.add_argument("--send-email-html", metavar="PATH", help="Send a pre-written HTML email file to all subscribers")
    parser.add_argument("--build-email", metavar="PATH", help="Generate email HTML from briefing markdown")
    parser.add_argument("--output", metavar="PATH", help="Output path for --build-email (default: <briefing>.email.html)")
    parser.add_argument("--read-url", metavar="URL", default="", help="URL for Read Online button")
    parser.add_argument("--pdf-url", metavar="URL", default="", help="URL for Download PDF button (omit to hide button)")
    parser.add_argument("--add", metavar="EMAIL", help="Add a new subscriber")
    parser.add_argument("--remove", metavar="EMAIL", help="Remove a subscriber")
    parser.add_argument("--list", action="store_true", help="List all subscribers")
    parser.add_argument("--serve", action="store_true", help="Start the API server")
    parser.add_argument("--preview", metavar="PATH", help="Preview HTML email from markdown file")

    args = parser.parse_args()

    if args.serve:
        run_server()
    elif args.send:
        result = send_briefing_to_all(
            args.send,
            read_url=args.read_url or "",
            pdf_url=args.pdf_url or "",
        )
        print(json.dumps(result, indent=2))
    elif args.send_email_html:
        result = send_email_from_html(args.send_email_html)
        print(json.dumps(result, indent=2))
    elif args.build_email:
        result = build_and_save_email(
            args.build_email,
            output_path=args.output or "",
            read_url=args.read_url or "",
            pdf_url=args.pdf_url or "",
        )
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
        _, html, _ = build_email_html(md, issue_number=1, read_url="#", pdf_url="#", recipient_name="Reader")
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
