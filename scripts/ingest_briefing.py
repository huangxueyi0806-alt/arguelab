#!/usr/bin/env python3
"""
ingest_briefing.py — Parse an ArgueLab briefing markdown, build content_json,
POST to Railway API, generate PDF, and upload to Supabase Storage.

Usage:
  python ingest_briefing.py <briefing.md>
  python ingest_briefing.py <briefing.md> --skip-pdf  (skip PDF generation)
"""

import sys
import os
import re
from pathlib import Path
from datetime import datetime
import requests
import yaml

# ── Config ──────────────────────────────────────────────────
RAILWAY_API = "https://arguelab-production.up.railway.app/api/issues"
SCRIPTS_DIR = Path(__file__).parent


def parse_frontmatter(text):
    """Extract YAML frontmatter if present."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            yaml_str = text[3:end].strip()
            body = text[end + 3:].strip()
            try:
                return yaml.safe_load(yaml_str) or {}, body
            except Exception:
                return {}, body
    return {}, text


def parse_header(body, frontmatter):
    """Extract header fields from the first lines of the briefing."""
    lines = body.split("\n")

    date_str = ""
    date_iso = ""
    title = ""
    topic = ""
    training_focus = ""

    # Line 1: "# ArgueLab — 今日训练简报 | June 18, 2026"
    for line in lines[:5]:
        if line.startswith("# "):
            m = re.search(r"\|\s*(.+)$", line)
            if m:
                date_str = m.group(1).strip()
                try:
                    dt = datetime.strptime(date_str, "%B %d, %Y")
                    date_iso = dt.strftime("%Y-%m-%d")
                except ValueError:
                    date_iso = datetime.now().strftime("%Y-%m-%d")
            break

    # "> **今日议题：**..."
    for line in lines[:10]:
        if "今日议题" in line:
            m = re.search(r"\*\*今日议题[：:]\s*\*\*(.+)", line)
            if m:
                title = m.group(1).strip()
            break

    # Chinese topic from title
    if "——" in title:
        topic = title.split("——")[0].strip()
    elif "—" in title:
        topic = title.split("—")[0].strip()

    # "> **训练重点：**..."
    for line in lines[:10]:
        if "训练重点" in line:
            m = re.search(r"\*\*训练重点[：:]\s*\*\*(.+)", line)
            if m:
                training_focus = m.group(1).strip()
            break

    # Use filename date as fallback
    if not date_iso:
        date_iso = datetime.now().strftime("%Y-%m-%d")

    issue_number = frontmatter.get("issue_number", 0)
    slug = date_iso

    return {
        "issue_number": issue_number,
        "slug": slug,
        "title": title,
        "topic": topic,
        "training_focus": training_focus,
        "date": date_str,
        "date_iso": date_iso,
    }


def split_sections(body):
    """Split the body into sections by ## N. headers."""
    sections = {}
    # Find all section headers
    pattern = r"^##\s+(\d+)\.\s+(.+)$"
    matches = list(re.finditer(pattern, body, re.MULTILINE))

    for i, m in enumerate(matches):
        section_num = int(m.group(1))
        section_title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        sections[section_num] = {"title": section_title, "content": content}

    return sections


def parse_background(section):
    """Parse Section 1: 今日议题背景."""
    content = section["content"]
    lines = content.split("\n")

    issue_statement = ""
    paragraphs = []
    framings = []
    why_this_issue = ""
    current_para = ""
    in_paragraphs = False
    in_framings = False
    in_why = False

    for line in content.split("\n"):
        line = line.strip()

        # Skip the opening line
        if "核心内容已整理" in line or "先建立 context" in line:
            continue
        if line.startswith("*核心内容") or line.startswith("核心内容"):
            continue

        # Issue statement
        if line.startswith("**议题：**") or line.startswith("**议题:**"):
            issue_statement = line.replace("**议题：**", "").replace("**议题:**", "").strip()
            continue

        # Why this issue
        if "为什么选这个议题" in line or "## 为什么选" in line:
            in_why = True
            in_paragraphs = False
            in_framings = False
            continue

        # Framings
        if "常见 framing" in line or "framing 方式" in line:
            in_framings = True
            in_paragraphs = False
            in_why = False
            continue

        # Framing items
        if in_framings and line.startswith("- "):
            item = line[2:].strip()
            # Parse: "*lump of labour fallacy*（劳动总量谬误——...）"
            en_term = ""
            cn_explanation = ""
            m = re.match(r"\*([^*]+)\*[（(](.+)[）)]", item)
            if m:
                en_term = m.group(1).strip()
                cn_explanation = m.group(2).strip()
            else:
                # Try simpler format
                parts = item.split("（", 1)
                if len(parts) == 2:
                    en_term = parts[0].strip().strip("*")
                    cn_explanation = parts[1].rstrip("）").strip()
                else:
                    en_term = item
            framings.append({"en_term": en_term, "cn_explanation": cn_explanation})
            continue

        # Why this issue content
        if in_why and line and not line.startswith("##"):
            if not why_this_issue:
                why_this_issue = line
            else:
                why_this_issue += " " + line
            continue

        # Regular paragraphs (after issue statement, before framings/why)
        if line and not in_framings and not in_why and not line.startswith("**议题"):
            if not line.startswith("##") and not line.startswith("**争议焦点"):
                if line.startswith("**争议焦点"):
                    continue
                if current_para:
                    current_para += " " + line
                else:
                    current_para = line
        elif current_para:
            paragraphs.append(current_para)
            current_para = ""

    if current_para:
        paragraphs.append(current_para)

    return {
        "issue_statement": issue_statement,
        "paragraphs": paragraphs[:3] if paragraphs else [content[:500]],
        "why_this_issue": why_this_issue,
        "framings": framings if framings else [],
    }


def parse_passage(section):
    """Parse Section 2: 外刊核心段落."""
    content = section["content"]

    # Extract source line
    source = ""
    source_title = ""
    source_name = ""
    source_url = ""
    source_date = ""
    adapted = True

    m = re.search(r"\*From:\s*(.+?)(?:\n|$)", content)
    if m:
        source = m.group(1).strip()
        if "adapted for training" in source:
            adapted = True
            source_clean = source.replace(" — adapted for training", "").replace(" - adapted for training", "")
            # Parse: "BBC News — "title" — June 17, 2026"
            parts = source_clean.split(" — ")
            if len(parts) >= 1:
                source_name = parts[0].strip()
            if len(parts) >= 2:
                title_part = parts[1].strip().strip('"')
                source_title = title_part
            if len(parts) >= 3:
                source_date = parts[2].strip()

    # Extract full text from blockquote (remove labels)
    full_text_lines = []
    reading_guide = ""
    argument_labels = []
    in_blockquote = False
    in_guide = False

    for line in content.split("\n"):
        line_stripped = line.strip()

        if "阅读指引" in line_stripped or "📖" in line_stripped:
            in_guide = True
            in_blockquote = False
            continue

        if in_guide:
            if line_stripped and not line_stripped.startswith("##"):
                if reading_guide:
                    reading_guide += " " + line_stripped
                else:
                    reading_guide = line_stripped
            continue

        # Blockquote lines
        if line_stripped.startswith("> "):
            in_blockquote = True
            content_line = line_stripped[2:]

            # Check for labels like **[Thesis · 主论点]**
            label_match = re.match(r"\*\*\[([\w-]+)\s*[·•]\s*([^\]]+)\]\*\*\s*(.*)", content_line)
            if not label_match:
                label_match = re.match(r"\[([\w-]+)\s*[·•]\s*([^\]]+)\]\s*(.*)", content_line)

            if label_match:
                label_type = label_match.group(1).lower().replace(" ", "_")
                label_cn = label_match.group(2).strip()
                label_text = label_match.group(3).strip()
                # Map label types
                type_map = {
                    "thesis": "thesis",
                    "premise": "premise",
                    "evidence": "evidence",
                    "counter-argument": "counter_argument",
                    "counter_argument": "counter_argument",
                    "conclusion": "conclusion",
                }
                color_map = {
                    "thesis": "#b8860b",
                    "premise": "#2e8b57",
                    "evidence": "#4682b4",
                    "counter_argument": "#b22222",
                    "conclusion": "#6a5acd",
                }
                normalized = type_map.get(label_type, label_type)
                start = len(" ".join(full_text_lines)) + (1 if full_text_lines else 0)
                full_text_lines.append(label_text)
                end = len(" ".join(full_text_lines))
                argument_labels.append({
                    "type": normalized,
                    "label_cn": label_cn,
                    "start": start,
                    "end": end,
                    "color": color_map.get(normalized, "#889DC4"),
                })
            else:
                full_text_lines.append(content_line)

    full_text = " ".join(full_text_lines)

    result = {
        "source": source if source else "Training passage · Editorial-style sample for expression practice",
        "full_text": full_text,
        "argument_labels": argument_labels,
        "reading_guide": reading_guide,
    }

    if adapted and source_name:
        result["adapted_for_training"] = True
        result["source_title"] = source_title
        result["source_name"] = source_name
        result["source_url"] = source_url
        result["source_date"] = source_date

    return result


def parse_expressions(section):
    """Parse Section 3: 5个可迁移表达."""
    content = section["content"]
    expressions = []

    # Split by expression headers
    blocks = re.split(r"###\s*表达\s*\d+", content)
    blocks = [b.strip() for b in blocks if b.strip()]

    for i, block in enumerate(blocks[:5]):
        lines = block.strip().split("\n")
        expr = {
            "index": i + 1,
            "phrase": "",
            "grammar_tag": "",
            "function_tag": "",
            "register": "",
            "sub_tag": "",
            "cn_explanation": "",
            "collocations": [],
            "example_sentence": "",
        }

        in_collocations = False
        in_example = False
        explanation_lines = []
        collocation_text = ""

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Phrase line: **`phrase`** `grammar_tag`
            phrase_match = re.match(r"\*\*`(.+?)`\*\*\s*`(.+?)`", stripped)
            if phrase_match and not expr["phrase"]:
                expr["phrase"] = phrase_match.group(1)
                expr["grammar_tag"] = phrase_match.group(2)
                continue

            # Function/register line: **`function · register · sub_tag`**
            tag_match = re.match(r"\*\*`(.+?)`\*\*", stripped)
            if tag_match and not expr["function_tag"]:
                tags = [t.strip() for t in tag_match.group(1).split("·")]
                if len(tags) >= 1:
                    expr["function_tag"] = tags[0]
                if len(tags) >= 2:
                    expr["register"] = tags[1]
                if len(tags) >= 3:
                    expr["sub_tag"] = tags[2]
                continue

            # Collocations
            if "常见搭配" in stripped:
                in_collocations = True
                collocation_text = stripped
                continue

            if "例句" in stripped:
                in_collocations = False
                in_example = True
                # Extract example from this line
                ex_match = re.search(r"\*\*(.+?)\*\*$", stripped)
                if ex_match:
                    expr["example_sentence"] = ex_match.group(1).strip()
                # Or italic format
                ex_match2 = re.search(r"\*(.+?)\*$", stripped)
                if ex_match2 and not expr["example_sentence"]:
                    expr["example_sentence"] = ex_match2.group(1).strip()
                continue

            if in_collocations and stripped:
                # Extract collocations from formats like:
                # "push back against the narrative / push back against..."
                collocation_text += " " + stripped
                continue

            if in_example and not expr["example_sentence"]:
                # Italic example
                ex_match = re.match(r"\*(.+?)\*$", stripped)
                if ex_match:
                    expr["example_sentence"] = ex_match.group(1).strip()
                continue

            # Explanation text (between tags and collocations/example)
            if not in_collocations and not in_example and not expr["cn_explanation"] and not stripped.startswith("**"):
                explanation_lines.append(stripped)
                continue

        # Process collocations
        if collocation_text:
            # Extract individual collocations
            raw = re.sub(r"\*\*常见搭配[：:]\s*\*\*", "", collocation_text)
            raw = re.sub(r"\*\*常见搭配[：:]\*\*", "", raw)
            parts = re.split(r"\s*/\s*", raw)
            expr["collocations"] = [p.strip() for p in parts if p.strip() and len(p.strip()) > 3]

        if explanation_lines:
            expr["cn_explanation"] = " ".join(explanation_lines)

        if expr["phrase"]:
            expressions.append(expr)

    return expressions


def parse_sentence_decon(section):
    """Parse Section 4: 高级句型拆解."""
    content = section["content"]

    target_sentence = ""
    structure_analysis = ""
    structure_template = ""
    grammar_points = []
    imitation_template = ""
    applicable_scenarios = ""
    reference_imitation = ""

    in_target = False
    in_structure = False
    in_template = False
    in_grammar = False
    in_imitation = False
    in_scenarios = False
    in_reference = False
    grammar_items = []

    lines = content.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # Target sentence
        if "目标句" in stripped or "**目标句**" in stripped:
            in_target = True
            continue
        if in_target and stripped.startswith(">"):
            target_sentence = stripped.lstrip("> ").strip()
            in_target = False
            continue

        # Structure analysis
        if "结构拆解" in stripped:
            in_structure = True
            in_grammar = False
            continue
        if in_structure and not stripped.startswith("**") and not stripped.startswith("##"):
            if not stripped.startswith("**结构模板"):
                structure_analysis += stripped + " "
            else:
                in_structure = False
                in_template = True
                continue

        # Structure template
        if in_template:
            if stripped.startswith("`"):
                structure_template = stripped.strip("`").strip()
                in_template = False
            continue
        if "结构模板" in stripped and not in_template:
            # Next line should be the template
            in_template = True
            continue

        # Grammar points
        if "语法要点" in stripped:
            in_grammar = True
            continue
        if in_grammar and stripped.startswith("- "):
            # Parse: "- **Title：** explanation"
            item = stripped[2:]
            m = re.match(r"\*\*(.+?)[：:]\s*\*\*\s*(.+)", item)
            if m:
                grammar_items.append({
                    "title": m.group(1).strip(),
                    "explanation": m.group(2).strip(),
                })
            continue
        if in_grammar and (stripped.startswith("**仿写") or "仿写模板" in stripped):
            in_grammar = False
            in_imitation = True
            continue

        # Imitation template
        if in_imitation:
            if stripped.startswith("`"):
                imitation_template = stripped.strip("`").strip()
                in_imitation = False
            elif "`" in stripped:
                # Template might be on the same line
                m = re.search(r"`(.+?)`", stripped)
                if m:
                    imitation_template = m.group(1).strip()
                    in_imitation = False
            continue
        if "仿写模板" in stripped and not in_imitation and not imitation_template:
            in_imitation = True
            # Try to extract from same line
            m = re.search(r"`(.+?)`", stripped)
            if m:
                imitation_template = m.group(1).strip()
                in_imitation = False
            continue

        # Applicable scenarios
        if "适用场景" in stripped:
            in_scenarios = True
            # Extract text after the colon from the same line
            m = re.search(r"适用场景[：:]\s*\*\*\s*(.+)", stripped)
            if m:
                applicable_scenarios = m.group(1).strip()
                in_scenarios = False
            continue
        if in_scenarios and stripped and not stripped.startswith("**"):
            applicable_scenarios += stripped + " "
            continue
        if in_scenarios and (stripped.startswith("**") or "你的仿写" in stripped):
            in_scenarios = False

        # Reference imitation
        if "你的仿写" in stripped or "参考仿写" in stripped:
            in_reference = True
            continue
        if in_reference and stripped.startswith("*"):
            reference_imitation += stripped.strip("*").strip() + " "
            continue

    return {
        "target_sentence": target_sentence.strip(),
        "structure_analysis": structure_analysis.strip(),
        "structure_template": structure_template.strip(),
        "grammar_points": grammar_items if grammar_items else [],
        "imitation_template": imitation_template.strip(),
        "applicable_scenarios": applicable_scenarios.strip(),
        "reference_imitation": reference_imitation.strip(),
    }


def parse_argument_chain(section):
    """Parse Section 5: 中文观点 → 英文 Argument Chain."""
    content = section["content"]

    cn_viewpoint = ""
    en_core_concept = ""
    causal_chain = []
    weighing = []
    sample_paragraph = ""

    in_cn = False
    in_en = False
    in_chain = False
    in_weighing = False
    in_sample = False
    chain_text = ""

    lines = content.split("\n")
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if "中文观点" in stripped or "🇨🇳" in stripped:
            in_cn = True
            continue
        if "English Core Concept" in stripped or "🏗️" in stripped:
            in_cn = False
            in_en = True
            continue
        if "Causal Chain" in stripped or "⛓️" in stripped:
            in_en = False
            in_chain = True
            continue
        if "Weighing" in stripped or "⚖️" in stripped:
            in_chain = False
            in_weighing = True
            continue
        if "Sample Argument Paragraph" in stripped or "✍️" in stripped:
            in_weighing = False
            in_sample = True
            continue

        # Skip code fences
        if stripped.startswith("```"):
            continue

        if in_cn and stripped.startswith(">"):
            cn_viewpoint = stripped.lstrip("> ").strip()
            in_cn = False
        elif in_cn and not stripped.startswith(">"):
            # Multi-line CN viewpoint
            cn_viewpoint += stripped + " "
        elif in_en:
            if stripped.startswith("*"):
                en_core_concept += stripped.strip("*").strip() + " "
            elif not en_core_concept:
                en_core_concept = stripped
        elif in_chain:
            chain_text += stripped + " "
        elif in_weighing:
            if stripped and not stripped.startswith("📌") and not stripped.startswith("*ArgueLab"):
                weighing.append(stripped)
        elif in_sample:
            sample_paragraph += stripped + " "

    # Parse causal chain
    if chain_text:
        # Parse arrow-separated chain
        if "→" in chain_text:
            steps = chain_text.split("→")
        elif "→" in chain_text:  # Full-width arrow
            steps = chain_text.split("→")
        else:
            steps = [chain_text]

        for step in steps:
            step = step.strip()
            if step:
                # Split into step name and explanation
                if "(" in step:
                    parts = step.split("(", 1)
                    causal_chain.append({
                        "step": parts[0].strip(),
                        "explanation": "(" + parts[1] if len(parts) > 1 else "",
                    })
                else:
                    causal_chain.append({
                        "step": step,
                        "explanation": "",
                    })

    # Process weighing sections — each collected paragraph is a complete weighing discussion
    weighing_sections = []
    for para in weighing:
        if not para.strip():
            continue
        # Split paragraph roughly in half for counter/rebuttal
        sentences = re.split(r'(?<=[.!?])\s+', para)
        if len(sentences) >= 2:
            mid = len(sentences) // 2
            counter = " ".join(sentences[:mid])
            rebuttal = " ".join(sentences[mid:])
        else:
            counter = para
            rebuttal = para
        weighing_sections.append({
            "counter_argument": counter,
            "your_rebuttal": rebuttal,
        })

    # Count expressions in sample paragraph
    expr_count = 0
    if sample_paragraph:
        expr_count = sample_paragraph.count("**")

    return {
        "cn_viewpoint": cn_viewpoint.strip(),
        "en_core_concept": en_core_concept.strip(),
        "causal_chain": causal_chain if causal_chain else [],
        "weighing": weighing_sections if weighing_sections else [],
        "sample_paragraph": sample_paragraph.strip(),
        "expression_count_used": expr_count // 2,  # Each bold has **...**
    }


def parse_output_tasks(section):
    """Parse Section 6: 输出任务."""
    content = section["content"]

    writing_task = ""
    writing_word_count = "280–320 词"
    speaking_task = ""
    speaking_duration = "1.5–2 分钟"
    structure_guide = []
    self_check = []

    in_writing = False
    in_speaking = False
    in_structure = False
    in_check = False

    lines = content.split("\n")
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if "写作任务" in stripped:
            in_writing = True
            in_speaking = False
            # Check for inline task
            m = re.search(r"写[作件]任务[^>]*>\s*(.+)", stripped)
            if m:
                writing_task = m.group(1).strip()
            continue

        if "口语任务" in stripped:
            in_speaking = True
            in_writing = False
            in_structure = False
            continue

        if "结构指引" in stripped:
            in_structure = True
            in_speaking = False
            in_check = False
            continue

        if "Self-check" in stripped or "清单" in stripped:
            in_check = True
            in_structure = False
            continue

        if in_writing and stripped.startswith(">"):
            writing_task = stripped.lstrip("> ").strip()
        elif in_speaking and stripped.startswith(">"):
            speaking_task = stripped.lstrip("> ").strip()
        elif in_structure and stripped.startswith("- "):
            item = stripped[2:]
            m = re.match(r"\*\*Step\s*(\d+)\s*[—–\-]\s*(.+?)[：:]\s*\*\*\s*(.+)", item)
            if m:
                structure_guide.append({
                    "step": f"Step {m.group(1)} — {m.group(2).strip()}",
                    "guide": m.group(3).strip(),
                })
            else:
                # Simpler format
                structure_guide.append({
                    "step": item[:80] if len(item) > 80 else item,
                    "guide": item,
                })
        elif in_check and stripped.startswith("- "):
            # Remove checkbox markers
            check_item = re.sub(r"^[-•]\s*[□☑✓✔✅]\s*", "", stripped)
            check_item = check_item.strip()
            if check_item and len(check_item) > 5:
                self_check.append(check_item)

    # Word count extraction
    wc_match = re.search(r"(\d+[–\-]\d+)\s*词", writing_task)
    if wc_match:
        writing_word_count = wc_match.group(1) + " 词"
        writing_task = re.sub(r"[（(]\s*建议\s*\d+[–\-]\d+\s*词[）)]", "", writing_task).strip()

    dur_match = re.search(r"(\d+[\.\d]*[–\-]\d+)\s*分钟", speaking_task)
    if dur_match:
        speaking_duration = dur_match.group(1) + " 分钟"
        speaking_task = re.sub(r"[（(]\s*建议\s*\d+[\.\d]*[–\-]\d+\s*分钟[）)]", "", speaking_task).strip()

    return {
        "writing_task": writing_task,
        "writing_word_count": writing_word_count,
        "speaking_task": speaking_task,
        "speaking_duration": speaking_duration,
        "structure_guide": structure_guide,
        "self_check": self_check,
    }


def build_content_json(md_path):
    """Build a complete content_json from a briefing markdown file."""
    text = Path(md_path).read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(text)
    header = parse_header(body, frontmatter)
    sections = split_sections(body)

    date_str = header["date"] if header["date"] else datetime.now().strftime("%B %d, %Y")

    content = {
        "header": header,
        "background": parse_background(sections.get(1, {"content": ""})),
        "passage": parse_passage(sections.get(2, {"content": ""})),
        "expressions": parse_expressions(sections.get(3, {"content": ""})),
        "sentence_deconstruction": parse_sentence_decon(sections.get(4, {"content": ""})),
        "argument_chain": parse_argument_chain(sections.get(5, {"content": ""})),
        "output_tasks": parse_output_tasks(sections.get(6, {"content": ""})),
        "footer": {
            "brand": "ArgueLab",
            "generated": f"generated {date_str}",
            "slogan": "Read like a scholar. Argue like a native.",
        },
    }

    return content


def post_to_railway(content_json, generate_pdf=True):
    """POST the content_json to the Railway backend API."""
    print(f"[ingest] Posting to {RAILWAY_API}...")
    body = {"content_json": content_json, "status": "draft"}
    if generate_pdf:
        body["generate_pdf"] = True
        print("[ingest] Auto-PDF generation requested")

    resp = requests.post(
        RAILWAY_API,
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=120,  # longer timeout for PDF generation
    )
    data = resp.json()
    if resp.status_code in (200, 201):
        # Accept both old format {"success": true, "issue": {...}} and new format (direct issue object)
        issue = data.get("issue", data)
        slug = issue.get("slug", content_json.get("header", {}).get("slug"))
        status = issue.get("status", "unknown")
        print(f"[ingest] Issue stored: {slug} (status: {status})")
        print(f"[ingest] PDF (on-demand): https://arguelab-production.up.railway.app/issues/{slug}/download")
        return issue
    elif resp.status_code == 409:
        print(f"[ingest] Issue already exists (409). Slug: {content_json['header']['slug']}")
        return {"slug": content_json["header"]["slug"], "already_exists": True}
    else:
        print(f"[ingest] Error ({resp.status_code}): {data}")
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python ingest_briefing.py <briefing.md> [--skip-pdf]")
        sys.exit(1)

    md_path = Path(sys.argv[1])
    if not md_path.exists():
        print(f"Error: File not found: {md_path}")
        sys.exit(1)

    skip_pdf = "--skip-pdf" in sys.argv

    print(f"[ingest] Processing: {md_path}")

    # Step 1: Build content_json
    print("[ingest] Building content_json...")
    content_json = build_content_json(md_path)
    slug = content_json["header"]["slug"]

    # Step 2: POST to Railway (backend handles PDF generation)
    issue = post_to_railway(content_json, generate_pdf=not skip_pdf)

    if issue is None:
        print("[ingest] Failed to create issue on Railway.")
        sys.exit(1)

    print(f"\n[ingest] Done! Web: https://arguelab-production.up.railway.app/issues/{slug}")
    if not skip_pdf:
        print(f"[ingest] PDF: https://arguelab-production.up.railway.app/issues/{slug}/download")


if __name__ == "__main__":
    main()
