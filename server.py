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
    date_str = datetime.now().strftime("%B %d, %Y")

    for line in md_text.split("\n"):
        stripped = line.strip()

        m = re.match(r'>\s*\*\*今日议题：\*\*\s*(.+)', stripped)
        if m:
            topic_line = m.group(1).strip()
            continue

        m = re.match(r'>\s*\*\*训练重点：\*\*\s*(.+)', stripped)
        if m:
            training_focus = m.group(1).strip()
            continue

        m = re.match(r'^#\s+ArgueLab.*\|\s*(.+)$', stripped)
        if m:
            date_str = m.group(1).strip()
            continue

        if len(practice_items) < 4:
            m = re.match(r'^-\s+\*([^*]+)\*[（(](.+)[）)]', stripped)
            if m:
                practice_items.append(f"如何理解 {m.group(1).strip()} — {m.group(2).strip()}")
                continue

    if not practice_items:
        for line in md_text.split("\n"):
            stripped = line.strip()
            if 'Step' in stripped and ('Thesis' in stripped or 'Causal' in stripped):
                item = re.sub(r'\*\*|Step \d+\s*[—–-]\s*', '', stripped).strip()
                if item and len(item) > 5:
                    practice_items.append(item)
                if len(practice_items) >= 3:
                    break

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

        # ── Card ──
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="620"'
        ' class="email-card" bgcolor="#0B0F14"'
        ' style="max-width:620px;border-radius:18px;border:1px solid rgba(148,163,184,0.16);overflow:hidden;">\n'

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

        '</table>\n'  # end card
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

    subs = load_subscribers()
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


# ── Issue Page Renderer ──

ISSUE_PAGE_CSS = """
  /* ── Design Tokens ── */
  :root {
    /* Surfaces */
    --bg: #0A0D12;
    --surface: #0D1117;
    --card-bg: #111820;
    --card-elevated: #151D28;
    /* Ink */
    --ink: #E2E5EC;
    --ink-dim: #B0B8C4;
    --ink-muted: #5A6375;
    /* Functional Module Colors */
    --color-context: #7B9CB8;
    --color-context-soft: rgba(123,156,184,0.10);
    --color-context-border: rgba(123,156,184,0.18);
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
    --color-output: #C9A96E;
    --color-output-soft: rgba(201,169,110,0.10);
    --color-output-border: rgba(201,169,110,0.20);
    --color-check: #7BA3A8;
    --color-check-soft: rgba(123,163,168,0.10);
    --color-check-border: rgba(123,163,168,0.18);
    /* Argument labels (keep existing) */
    --thesis: #F0C060;
    --premise: #78C0E0;
    --evidence: #A0D890;
    --counter: #E088A0;
    --conclusion: #D0A8F0;
    /* Borders */
    --border: rgba(136,157,196,0.08);
    --border-strong: rgba(136,157,196,0.15);
    --divider: rgba(136,157,196,0.06);
    /* Spacing */
    --section-gap: 72px;
    --card-radius: 14px;
    --card-padding: 26px 30px;
  }

  /* ── Reset & Base ── */
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html { scroll-behavior: smooth; }
  body {
    font-family: Georgia, 'Times New Roman', 'PingFang SC', 'Microsoft YaHei', serif;
    background: var(--bg);
    color: var(--ink);
    line-height: 1.85;
    font-size: 16px;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }

  /* ── Layout ── */
  .issue-shell {
    max-width: 1180px;
    margin: 0 auto;
    padding: 96px 32px 80px;
    display: grid;
    grid-template-columns: 220px minmax(0, 760px);
    gap: 72px;
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
    max-width: 760px;
    margin: 0 auto;
  }

  /* Mobile: stack layout */
  @media (max-width: 860px) {
    .issue-shell {
      display: block;
      padding: 72px 20px 56px;
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
    color: #7E8A9D;
    font-size: 13px;
  }
  .toc-label {
    font-size: 11px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: #8A93A5;
    margin-bottom: 24px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  }
  .toc-list { list-style: none; padding: 0; }
  .toc-link {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 0;
    color: #748095;
    text-decoration: none;
    transition: color .2s ease, transform .2s ease;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    font-size: 13px;
  }
  .toc-num {
    font-size: 11px;
    font-weight: 700;
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    color: inherit;
    opacity: 0.5;
  }
  .toc-link::before {
    content: "";
    width: 12px;
    height: 1px;
    background: rgba(148, 163, 184, 0.4);
    flex-shrink: 0;
  }
  .toc-link:hover { color: #D8E2F0; }
  .toc-link.active { color: #D8E2F0; }
  .toc-link.active::before {
    background: #8FA7C8;
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
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    color: var(--ink-muted);
    text-decoration: none;
    border: 1px solid var(--border);
    transition: all 0.2s ease;
  }
  .toc-mobile .toc-chip.active { color: var(--ink); border-color: var(--border-strong); }
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
    padding: 40px 0 56px;
    border-bottom: 1px solid rgba(148, 163, 184, 0.12);
    margin-bottom: 64px;
  }
  .issue-kicker {
    font-size: 12px;
    letter-spacing: 0.18em;
    color: #8FA7C8;
    margin-bottom: 18px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    text-transform: uppercase;
  }
  .issue-hero h1 {
    font-family: Georgia, "Times New Roman", serif;
    font-size: clamp(34px, 5vw, 54px);
    line-height: 1.08;
    letter-spacing: -0.035em;
    color: #E8EDF5;
    margin: 0 0 18px;
  }
  .issue-title {
    font-family: Georgia, "Times New Roman", serif;
    font-size: 18px;
    line-height: 1.6;
    color: #B8C3D4;
    font-style: italic;
    margin: 0 auto 16px;
    max-width: 720px;
  }
  .issue-meta {
    font-size: 14px;
    line-height: 1.6;
    color: #7E8A9D;
    margin: 0;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  }

  /* ── Issue Sections ── */
  .issue-section {
    margin-bottom: var(--section-gap);
    scroll-margin-top: 60px;
  }
  .issue-section:last-of-type { margin-bottom: 48px; }

  /* Section Heading with color badge */
  .section-heading {
    display: flex;
    align-items: baseline;
    gap: 14px;
    margin-bottom: 28px;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--divider);
  }
  .section-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    border-radius: 10px;
    font-size: 14px;
    font-weight: 700;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    flex-shrink: 0;
  }
  .section-heading h2 {
    font-size: 20px;
    font-weight: 700;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    letter-spacing: -0.2px;
    line-height: 1.3;
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
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    border: 1px solid rgba(148, 163, 184, 0.22);
    background: rgba(15, 23, 42, 0.42);
  }
  .source-badge.source {
    color: #93C5FD;
    border-color: rgba(147, 197, 253, 0.26);
    background: rgba(59, 130, 246, 0.08);
  }
  .source-badge.training {
    color: #F0C987;
    border-color: rgba(240, 201, 135, 0.26);
    background: rgba(245, 158, 11, 0.08);
  }
  .source-badge.ai {
    color: #C4B5FD;
    border-color: rgba(196, 181, 253, 0.26);
    background: rgba(139, 92, 246, 0.08);
  }
  .source-badge.practice {
    color: #86EFAC;
    border-color: rgba(134, 239, 172, 0.24);
    background: rgba(34, 197, 94, 0.08);
  }
  .source-note {
    font-size: 13px;
    line-height: 1.7;
    color: #8390A3;
    margin-top: -6px;
    margin-bottom: 18px;
  }

  /* ── Source List (end of page) ── */
  .source-list {
    margin-top: 18px;
    padding: 14px 16px;
    border: 1px solid rgba(148, 163, 184, 0.14);
    background: rgba(15, 23, 42, 0.30);
    border-radius: 12px;
  }
  .source-list-title {
    font-size: 11px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: #8FA7C8;
    margin-bottom: 8px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  }
  .source-list ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .source-list li {
    font-size: 13px;
    line-height: 1.65;
    color: #95A1B4;
    padding: 3px 0;
  }

  /* ── Content Cards ── */
  .content-card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--card-radius);
    padding: var(--card-padding);
    margin-bottom: 24px;
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
  .cn-body strong { color: var(--ink); }
  .en-body {
    font-size: 16px;
    color: var(--ink);
    line-height: 1.8;
    margin-bottom: 16px;
    font-family: Georgia, 'Times New Roman', 'PingFang SC', serif;
  }
  .en-body strong { color: var(--ink); }

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
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  }

  /* ── Passage Block ── */
  .passage-block {
    background: var(--card-elevated);
    border: 1px solid var(--color-passage-border);
    border-radius: var(--card-radius);
    padding: 28px 30px;
    margin-bottom: 0;
    border-left: 3px solid var(--color-passage);
  }
  .passage-block p {
    font-size: 16px;
    line-height: 1.9;
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
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    letter-spacing: 0.3px;
    text-transform: uppercase;
    vertical-align: middle;
    position: relative;
    top: -1px;
  }
  .arg-thesis { background: rgba(240,192,96,0.12); color: var(--thesis); }
  .arg-premise { background: rgba(120,192,224,0.12); color: var(--premise); }
  .arg-evidence { background: rgba(160,216,144,0.12); color: var(--evidence); }
  .arg-counter { background: rgba(224,136,160,0.12); color: var(--counter); }
  .arg-conclusion { background: rgba(208,168,240,0.12); color: var(--conclusion); }

  /* ── Reading Guide (callout) ── */
  .guide-block {
    background: var(--color-passage-soft);
    border-left: 3px solid var(--color-passage);
    padding: 18px 22px;
    border-radius: 0 10px 10px 0;
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
    padding: 24px 28px;
    margin-bottom: 20px;
    border-left: 3px solid var(--color-expression);
    transition: border-color 0.2s ease;
  }
  .expr-card:last-child { margin-bottom: 0; }
  .expr-card .expr-num {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-expression);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 6px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  }
  .expr-card .expr-phrase {
    font-size: 17px;
    font-weight: 700;
    color: var(--ink);
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', 'JetBrains Mono', monospace;
    margin-bottom: 8px;
    letter-spacing: -0.2px;
  }
  .expr-card .expr-tags {
    font-size: 11px;
    color: var(--color-expression);
    margin-bottom: 12px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
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
    padding: 24px 28px;
    border-left: 3px solid var(--color-sentence);
  }
  .target-sentence-card .ts-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-sentence);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 10px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
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
    padding: 20px 24px;
  }
  .why-card .why-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-sentence);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 8px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
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
    padding: 20px 24px;
  }
  .structure-card .struct-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-sentence);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 8px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
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
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  }

  /* Grammar mini-cards */
  .grammar-mini-card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 12px;
    border-left: 3px solid var(--color-sentence);
  }
  .grammar-mini-card:last-child { margin-bottom: 0; }
  .grammar-mini-card .gm-title {
    font-size: 13px;
    font-weight: 700;
    color: var(--ink);
    margin-bottom: 6px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  }
  .grammar-mini-card .gm-body {
    font-size: 14px;
    color: var(--ink-dim);
    line-height: 1.7;
  }
  .grammar-mini-card .gm-code {
    display: block;
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    font-size: 13px;
    background: rgba(0,0,0,0.25);
    padding: 10px 14px;
    border-radius: 6px;
    color: var(--ink-dim);
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
  }
  .template-card .tpl-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-sentence);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 10px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  }
  .template-card .tpl-code {
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    font-size: 13px;
    background: rgba(0,0,0,0.25);
    padding: 14px 18px;
    border-radius: 8px;
    color: var(--ink-dim);
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
  }
  .imitation-card .imit-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-sentence);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 8px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
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
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    margin-top: 8px;
  }

  /* ── Argument Chain ── */
  .chain-flow {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .chain-step {
    background: var(--card-bg);
    border: 1px solid var(--color-chain-border);
    border-radius: var(--card-radius);
    padding: 20px 24px;
    border-left: 3px solid var(--color-chain);
  }
  .chain-step .step-label {
    font-size: 11px;
    font-weight: 700;
    color: var(--color-chain);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 8px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
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
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    font-size: 12px;
    background: rgba(0,0,0,0.25);
    padding: 12px 16px;
    border-radius: 6px;
    color: var(--ink-dim);
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
  }
  .weighing-card .weigh-label {
    font-size: 11px;
    font-weight: 700;
    color: var(--color-chain);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 12px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
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
    padding: 24px 28px;
    border-left: 3px solid var(--color-chain);
  }
  .sample-paragraph-card .sp-label {
    font-size: 11px;
    font-weight: 700;
    color: var(--color-chain);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 10px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
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
  }
  .task-card .task-type {
    font-size: 11px;
    font-weight: 700;
    color: var(--color-output);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 12px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
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
  }
  .guide-card .guide-label {
    font-size: 11px;
    font-weight: 700;
    color: var(--color-output);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 16px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
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
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
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
  }
  .check-card .check-label {
    font-size: 11px;
    font-weight: 700;
    color: var(--color-check);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 16px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
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
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 12px;
    padding: 28px;
    background: rgba(255,255,255,0.015);
  }
  .task-block .task-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
  }
  .task-block .task-header .task-type {
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--accent);
    background: rgba(136,157,196,0.12);
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
    background: rgba(255,255,255,0.03);
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
    border: 1px solid rgba(255,215,0,0.15);
    border-radius: 12px;
    background: linear-gradient(135deg, rgba(255,215,0,0.04), rgba(255,215,0,0.01));
  }
  .premium-hint-card .ph-label {
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #d4a853;
    margin-bottom: 12px;
  }
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
    content: '—';
    position: absolute;
    left: 0;
    color: var(--ink-muted);
  }

  /* ── Code / Template / Pre ── */
  code {
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: rgba(255,255,255,0.06);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.9em;
    color: var(--ink-dim);
  }
  pre {
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: rgba(0,0,0,0.25);
    padding: 16px 20px;
    border-radius: 8px;
    font-size: 13px;
    line-height: 1.65;
    overflow: hidden;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
    color: var(--ink-dim);
  }
  .template-box {
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    font-size: 13px;
    background: rgba(0,0,0,0.25);
    padding: 14px 18px;
    border-radius: 8px;
    color: var(--ink-dim);
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
    font-family: Georgia, 'Times New Roman', serif;
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
    font-family: system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif;
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
  }
  .callout-box strong { color: var(--ink); }

  /* ── Strong / Emphasis ── */
  strong { font-weight: 700; color: var(--ink); }
  em { font-style: italic; }

  /* ── Responsive ── */
  @media (max-width: 960px) {
    .issue-toc { display: none; }
    .toc-mobile { display: block; }
    .issue-shell { padding: 72px 20px 56px; }
    .issue-main { max-width: 100%; padding: 0; }
  }

  @media (max-width: 640px) {
    .issue-shell { padding: 72px 16px 56px; }
    .issue-main { padding: 0; }
    .issue-hero { padding: 40px 0 28px; }
    .issue-hero h1 { font-size: 22px; }
    .issue-title { font-size: 14px; }
    .issue-section { margin-bottom: 52px; }
    .section-heading { gap: 10px; }
    .section-heading h2 { font-size: 17px; }
    .section-badge { width: 30px; height: 30px; font-size: 12px; border-radius: 8px; }
    .content-card { padding: 20px 18px; }
    .passage-block { padding: 20px 18px; }
    .task-grid { grid-template-columns: 1fr; }
    .expr-card { padding: 20px 18px; }
    .chain-step { padding: 18px 18px; }
    .grammar-mini-card { padding: 14px 16px; }
    .target-sentence-card { padding: 18px 20px; }
    .target-sentence-card .ts-text { font-size: 15px; }
    .check-card { padding: 20px 18px; }
    .guide-card { padding: 20px 18px; }
    .sample-paragraph-card { padding: 20px 18px; }
    .weighing-card { padding: 20px 18px; }
    body { font-size: 15px; }
    .cn-body, .en-body { font-size: 15px; }
    .toc-mobile { padding: 8px 12px; }
    .toc-mobile .toc-chip { padding: 5px 12px; font-size: 11px; margin-right: 6px; }
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
      --section-gap: 32px;
    }
    body { font-size: 11pt; }
    .issue-shell { display: block; max-width: 100%; padding: 0; }
    .issue-toc, .toc-mobile { display: none !important; }
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
"""


def _render_issue_page(md_text: str, issue_date: str = "") -> str:
    """Convert ArgueLab v2 briefing markdown into a full self-contained dark-themed HTML page.

    This renders the 6-pane briefing as a beautiful standalone web page with:
    - Functional color system per module
    - Card-based content chunking
    - Sticky TOC navigation
    - Proper typography and visual rhythm
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

    # ── Render HTML ──
    html_parts = []

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
        '<aside class="issue-toc"><div class="toc-label">In This Issue</div>'
        '<ul class="toc-list">{}</ul></aside>'.format("".join(toc_items))
    )

    # Mobile TOC (inside main content, sticky at top)
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

    # Mobile TOC (inside main content, sticky at top)
    html_parts.insert(0, mobile_toc)
    html_parts.append(f'''<div class="issue-hero">
  <div class="issue-kicker">{_escape_html(issue_number)}</div>
  <h1>{_escape_html(title)}</h1>
  <p class="issue-title">{_escape_html(topic_line)}</p>
  <p class="issue-meta">{_escape_html(date_str)} &middot; {_escape_html(training_focus)}</p>
</div>''')

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

        # For structured sections (passage, expression, sentence, chain, output),
        # combine all text into one block so specialized renderers can detect the full structure.
        # Only context (section 0) uses paragraph-by-paragraph rendering.
        if mod in ("passage", "expression", "sentence", "chain", "output"):
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
        src_items = "".join(f"<li>{_escape_html(s)}</li>" for s in sources if s)
        if src_items:
            html_parts.append(
                '<div class="source-list">'
                '<div class="source-list-title">Sources Used in This Issue</div>'
                f'<ul>{src_items}</ul>'
                '</div>'
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

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{_escape_html(topic_line or "ArgueLab Training Briefing")}</title>
<style>{ISSUE_PAGE_CSS}</style>
</head>
<body>
<div class="issue-shell">
{toc_desktop}
<main class="issue-main">
{body}
</main>
</div>
{toc_js}
</body>
</html>'''


def _render_paragraph(text: str, module_type: str = "context") -> str:
    """Render a paragraph block with intelligent formatting.

    Detects special content types:
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

    # --- Passage block: starts with [Thesis, [Premise, etc (with optional > blockquote prefix and ** bold) ---
    if re.match(r'^(> )?(\*\*)?\[(Thesis|Premise|Evidence|Counter-?argument|Conclusion)', first_line):
        return _render_passage_block(text)
    # Handle combined passage section text that may contain multiple blocks
    if module_type == "passage" and re.search(r'(?:^|\n)(?:> )?(?:\*\*)?\[(Thesis|Premise|Evidence|Counter-?argument|Conclusion)', text):
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
    if module_type == "expression" and re.search(r'^###\s*表达\s*\d+', text, re.MULTILINE):
        return _render_expression_section(text)
    if re.match(r'^###\s*表达\s*\d+', first_line):
        return _render_expression_card(text)

    # --- Sentence deconstruction: contains 目标句 / 结构拆解 / 语法点 ---
    if "**目标句**" in text or "**目标句：**" in text or "**结构拆解**" in text or "**结构拆解：**" in text or "**语法点" in text or "**语法要点" in text:
        return _render_sentence_decon(text)

    # --- Output tasks: contains 写作任务 / 口语任务 / Task 1 / Task 2 / 结构引导 / 自我检查 ---
    # NOTE: must check BEFORE argument chain, since output text may contain "Weighing" etc.
    if ("**写作任务" in text or "**口语任务" in text or
        "**结构指引" in text or "**结构引导" in text or
        "**Self-check" in text or "**自我检查" in text or
        "### Task 1" in text or "### Task 2" in text or
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

        # "Argument Structure" header
        if s.startswith("**Argument Structure") or s == "**Argument Structure 标注**":
            i += 1
            continue

        # Reading guide
        if s.startswith("📖") or s.startswith("**📖"):
            guide_lines = []
            guide_lines.append(s)
            j = i + 1
            while j < len(lines) and lines[j].strip() and not lines[j].strip().startswith("---"):
                guide_lines.append(lines[j].strip())
                j += 1
            guide_text = "\n".join(guide_lines)
            guide_text = _markdown_inline_to_html(guide_text)
            guide_text = re.sub(r'<strong>.*?📖.*?</strong>\s*', '', guide_text)
            guide_text = re.sub(r'📖\s*\*\*.*?\*\*\s*', '', guide_text)
            parts.append(f'<div class="guide-block">{guide_text}</div>')
            i = j
            continue

        # Horizontal rule
        if s == "---":
            i += 1
            continue

        # Passage block: starts with > **[Thesis or > [Thesis or [Thesis (after strip)
        # Also handles the case where the passage has already had > stripped
        clean = s.lstrip("> ").lstrip("*")
        if re.match(r'^\[(Thesis|Premise|Evidence|Counter-?argument|Conclusion)', clean):
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
    """Render the argument-labeled passage block."""
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

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("*Source:") or line.startswith("*From:") or line.startswith("*Adapted"):
            source_line = line.strip("*").strip()
            continue

        # Replace argument labels with colored spans
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

        result_parts.append(line)

    source_html = f'<p class="source-line">{_escape_html(source_line)}</p>' if source_line else ""
    body_html = "<p>" + " ".join(result_parts) + "</p>"

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
        if re.match(r'^###\s*表达\s*\d+', s):
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
    """Render a single expression card (### 表达 N).

    Expected format:
    ### 表达 N
    **紧凑标签：**`tag1 · tag2 · tag3`

    **`phrase`** `register label`

    - CN explanation text
    - **常见搭配：** collocation examples
    - **例句：** example sentence
    """
    phrase = ""
    tags = ""
    cn_explanation = ""
    collocations = ""
    example = ""
    card_num = ""

    lines = text.split("\n")
    i = 0
    current_field = None  # Track which field we're currently appending to
    while i < len(lines):
        s = lines[i].strip()

        # Extract card number from ### header
        if s.startswith("###"):
            m = re.match(r'^###\s*表达\s*(\d+)', s)
            if m:
                card_num = m.group(1)
            i += 1
            continue

        # Skip empty lines
        if not s:
            i += 1
            continue

        # Compact tags line: **紧凑标签：**`...`
        if s.startswith("**紧凑标签：**"):
            tags = re.sub(r'\*\*紧凑标签：\*\*\s*', '', s)
            # Strip backticks
            tags = tags.strip("`")
            current_field = "tags"
            i += 1
            continue

        # Phrase line: **`phrase`** `register` (or similar)
        # Can be: **`conflate [A] with [B]`** `verb phrase`
        if s.startswith("**`") or (s.startswith("**") and "`" in s[:40]):
            # Extract the phrase from between the backticks inside bold
            m = re.match(r'\*\*`(.+?)`\*\*', s)
            if m:
                phrase = m.group(1)
            else:
                # Fallback: strip all ** markers
                phrase = re.sub(r'\*\*', '', s)
                # Remove trailing register label
                phrase = re.sub(r'`[^`]*`\s*$', '', phrase).strip()
            current_field = "phrase"
            i += 1
            continue

        # Bullet lines: content for explanation, collocations, or example
        if s.startswith("- "):
            content = s[2:]
            # Check if it's a collocation line
            if re.match(r'\*\*常见搭配[：:]', content) or re.match(r'\*\*Collocations?[：:]', content, re.IGNORECASE):
                collocations = re.sub(r'\*\*常见搭配[：:]\*\*\s*', '', content)
                collocations = re.sub(r'\*\*Collocations?[：:]\*\*\s*', '', collocations, flags=re.IGNORECASE)
                current_field = "colloc"
            elif re.match(r'\*\*例句[：:]', content) or re.match(r'\*\*Example[：:]', content, re.IGNORECASE):
                example = re.sub(r'\*\*例句[：:]\*\*\s*', '', content)
                example = re.sub(r'\*\*Example[：:]\*\*\s*', '', example, flags=re.IGNORECASE)
                current_field = "example"
            elif not cn_explanation:
                cn_explanation = content
                current_field = "cn"
            elif not collocations:
                collocations = content
                current_field = "colloc"
            else:
                example = content
                current_field = "example"
            i += 1
            continue

        # Continuation lines (non-bullet, non-header) — append to current field
        if current_field == "cn" and s:
            cn_explanation += " " + s
        elif current_field == "colloc" and s:
            collocations += " " + s
        elif current_field == "example" and s:
            example += " " + s
        # else: skip unrecognized lines

        i += 1

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

        # Target sentence - inline format: **目标句：** text
        if s.startswith("**目标句：**"):
            target_sentence = re.sub(r'\*\*目标句：\*\*\s*', '', s)
            # Also consume the blockquote content on next lines
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith(">"):
                target_sentence += " " + lines[j].strip()[1:].strip()
                j += 1
            i = j
            continue

        # Target sentence - block format: **目标句** on one line, > on next
        if s.startswith("**目标句**") and not s.startswith("**目标句：**"):
            current_mode = "target"
            i += 1
            # Consume blockquote lines
            while i < len(lines) and lines[i].strip().startswith(">"):
                target_sentence += " " + lines[i].strip()[1:].strip()
                i += 1
            continue

        if s.startswith("**结构拆解：**") or s.startswith("**结构拆解**"):
            if current_mode == "grammar" and current_grammar_title:
                grammar_points.append((current_grammar_title, " ".join(current_grammar_body)))
                current_grammar_body = []
            # Handle both "**结构拆解：** text" and "**结构拆解**" (bare header, content on next line)
            structure_analysis = re.sub(r'\*\*结构拆解[：:]?\*\*\s*', '', s)
            if not structure_analysis.strip():
                # Content is on next lines
                current_mode = "structure"
                i += 1
                j = i
                while j < len(lines) and lines[j].strip() and not lines[j].strip().startswith("**"):
                    structure_analysis += " " + lines[j].strip()
                    j += 1
                i = j
            else:
                structure_analysis = structure_analysis.strip()
                current_mode = "structure"
                i += 1
                # Also consume continuation lines
                j = i
                while j < len(lines) and lines[j].strip() and not lines[j].strip().startswith("**"):
                    structure_analysis += " " + lines[j].strip()
                    j += 1
                i = j
            continue

        if s.startswith("**结构模板**") or s.startswith("**结构模板：**"):
            if current_mode == "grammar" and current_grammar_title:
                grammar_points.append((current_grammar_title, " ".join(current_grammar_body)))
                current_grammar_body = []
            current_mode = "template"
            template_text = re.sub(r'\*\*结构模板\*\*:?\s*', '', s)
            if not template_text.strip():
                # Template is on next lines (code block)
                j = i + 1
                template_lines = []
                while j < len(lines):
                    ls = lines[j].strip()
                    if ls.startswith("```"):
                        j += 1
                        continue
                    if ls.startswith("**"):
                        break
                    if ls:
                        template_lines.append(ls)
                    j += 1
                template_text = "\n".join(template_lines)
                i = j
            else:
                # Template is inline in backticks
                template_text = template_text.strip("`")
                i += 1
            continue

        if s.startswith("**语法点") or s.startswith("**语法要点"):
            current_mode = "grammar"
            i += 1
            continue

        if s.startswith("**仿写模板：**") or s.startswith("**仿写模板**") or s.startswith("**模仿模板：**") or s.startswith("**模仿模板**"):
            if current_mode == "grammar" and current_grammar_title:
                grammar_points.append((current_grammar_title, " ".join(current_grammar_body)))
                current_grammar_body = []
            current_mode = "imitation"
            # Check if content is inline (e.g. **模仿模板：** text)
            inline = re.sub(r'\*\*(?:仿写模板|模仿模板)[：:]?\*\*\s*', '', s)
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
                    if ls.startswith("**"):
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

        if s.startswith("**仿写场景：**") or s.startswith("**仿写场景**") or s.startswith("**适用场景：**") or s.startswith("**适用场景**"):
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
                if ls.startswith("**") or ls.startswith("🏗️") or ls.startswith("🇨🇳"):
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

        if ("核心概念" in s[:30] and "**" in s[:30]) or s.startswith("🏗️") or "English Core Concept" in s or ("Core Concept" in s and "**" in s[:5]) or s.startswith("**EN Core**") or s.startswith("**EN Core：**"):
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
                if ls.startswith("**") or ls.startswith("⛓️"):
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
                if ls.startswith("**") or ls.startswith("⚖️") or ls.startswith("✍️"):
                    break
                causal_chain += "\n" + ls
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
                if ls.startswith("**示范段落：**") or ls.startswith("**示范段落**") or ls.startswith("✍️") or ls.startswith("**✍️") or "Sample Argument Paragraph" in ls or "Sample Paragraph" in ls[:40] or ls.startswith("📌") or ls.startswith("*ArgueLab"):
                    break
                if ls == "" and para:
                    weighing_texts.append(" ".join(para))
                    para = []
                elif ls and not ls.startswith("⚖️"):
                    para.append(ls)
                j += 1
            if para:
                weighing_texts.append(" ".join(para))
            i = j
            continue

        if s.startswith("**示范段落：**") or s.startswith("**示范段落**") or s.startswith("✍️") or s.startswith("**✍️") or "Sample Argument Paragraph" in s or "Sample Paragraph" in s[:40]:
            current_mode = "sample"
            i += 1
            j = i
            while j < len(lines):
                ls = lines[j].strip()
                if ls.startswith("📌") or ls.startswith("**📌") or ls.startswith("*ArgueLab"):
                    sample_note = re.sub(r'\*\*', '', ls).strip("📌").strip()
                    j += 1
                    break
                if ls:
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

        # Detect task headers: ### Task 1: ... or ### Task 2: ...
        if s.startswith("### Task 1") or s.startswith("### Task 2"):
            if current_task:
                tasks.append(current_task)
            # Determine task type from header
            task_type = "Writing Task" if "写作" in s or "Writing" in s else "Speaking Task"
            current_task = {"type": task_type, "prompt": "", "guide": [], "check": [], "meta": ""}
            current_section = "task1" if "Task 1" in s[:15] else "task2"
            current_mode = None
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

        # Legacy format: **口语任务...**
        if s.startswith("**口语任务") and not any(t["type"] == "Speaking Task" for t in tasks):
            if not current_task:
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
        if s.startswith("**题目：**") or s.startswith("**题目**"):
            current_mode = "prompt"
            # Check if prompt text is inline
            inline = re.sub(r'\*\*题目[：:]?\*\*\s*', '', s)
            if inline.strip():
                current_task["prompt"] = inline.strip()
            i += 1
            continue

        if s.startswith("**结构引导：**") or s.startswith("**结构引导**") or s.startswith("**结构指引"):
            current_mode = "guide"
            i += 1
            continue

        if s.startswith("**Self-Check") or s.startswith("**Self-check") or s.startswith("**自我检查"):
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
        # Fallback: render as plain paragraphs
        return _render_paragraph(text, "output")

    return "\n".join(html)


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
        elif self.path.startswith("/issues/"):
            self._serve_issue_page()
        elif self.path == "/api/subscribers":
            subs = load_subscribers()
            self._send_json({"count": len(subs), "subscribers": [{k: s[k] for k in ("email","name","subscribed_at")} for s in subs]})
        elif self.path == "/api/health":
            self._send_json({"status": "ok", "subscribers": len(load_subscribers()), "email_configured": bool(RESEND_API_KEY or SMTP_HOST)})
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
        
        if not briefing_file.exists():
            # Try listing what's available
            available = sorted(briefing_dir.glob("*.md")) if briefing_dir.exists() else []
            available_dates = [f.stem.replace("-briefing", "") for f in available]
            self.send_response(404)
            self.end_headers()
            msg = f"Issue not found for {issue_date}. Available: {', '.join(available_dates[:5])}"
            self.wfile.write(msg.encode("utf-8"))
            return
        
        # PDF download — generate PDF on the fly
        if is_pdf:
            self._serve_pdf(briefing_file, issue_date)
            return
        
        # Render issue page
        try:
            md_text = briefing_file.read_text(encoding="utf-8")
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

        script_path = BASE_DIR / "scripts" / "render-pdf.js"
        if not script_path.exists():
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"PDF renderer script not found. Install Node + Puppeteer.")
            return

        # Generate PDF
        try:
            result = subprocess.run(
                [node_exe, str(script_path), str(briefing_file), str(pdf_path)],
                capture_output=True, text=True, timeout=60
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
