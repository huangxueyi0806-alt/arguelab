#!/usr/bin/env python3
"""Quick parser: briefing.md → content_json → PUT to Railway"""

import sys, re, json, requests, pathlib

RAILWAY_API = "https://arguelab-production.up.railway.app/api/issues"


def parse_briefing(md_path):
    text = pathlib.Path(md_path).read_text(encoding="utf-8")
    
    # Split by ## headers
    sections = {}
    current_sec = None
    current_content = []
    for line in text.split("\n"):
        if line.startswith("## ") and not line.startswith("### "):
            if current_sec is not None:
                sections[current_sec] = "\n".join(current_content).strip()
            current_sec = line[3:].strip()
            current_content = []
        elif current_sec is not None:
            current_content.append(line)
    if current_sec is not None:
        sections[current_sec] = "\n".join(current_content).strip()
    
    # --- Header ---
    title = ""
    bg_sec = sections.get("今日议题背景", "")
    tm = re.search(r"\*\*议题[：:]\s*\*\*\s*(.+?)(?:\n|$)", bg_sec)
    if tm:
        title = tm.group(1).strip()
    
    date_iso = "2026-06-30"
    dm = re.search(r"#\s+ArgueLab.*([\d]{4}-[\d]{2}-[\d]{2})", text)
    if dm:
        date_iso = dm.group(1)
    
    header = {
        "issue_number": 0,
        "slug": date_iso,
        "title": title,
        "topic": title.split("——")[0].strip() if "——" in title else title[:40],
        "training_focus": "Disaster governance / Building safety / Government accountability",
        "date": date_iso,
        "date_iso": date_iso,
    }
    
    # --- Background ---
    bg_controversy = []
    bg_why = ""
    framings = []
    
    in_controversy = False
    in_why = False
    in_framing = False
    bg_section = sections.get("今日议题背景", "")
    
    for line in bg_section.split("\n"):
        s = line.strip()
        if "**争议：**" in s:
            in_controversy = True; continue
        if "**为什么选这个议题：**" in s:
            in_controversy = False; in_why = True
            bg_why = re.sub(r"\*\*为什么选这个议题[：:]\*\*\s*", "", s).strip()
            continue
        if "**Framing 提示：**" in s:
            in_why = False; in_framing = True; continue
        if in_controversy and s.startswith("- "):
            bg_controversy.append(s[2:].strip())
        elif in_controversy and s.startswith("-"):
            bg_controversy.append(s[1:].strip())
        elif in_why and s:
            bg_why += " " + s
        elif in_framing and s.startswith("- "):
            item = s[2:].strip()
            en, cn = item.split("从", 1) if "从" in item else (item, "")
            framings.append({"en_term": en.strip(), "cn_explanation": cn.strip()})
    
    # Extract paragraphs (背景)
    paras = []
    bg_match = re.search(r"\*\*背景[：:]\*\*\s*(.+?)(?:\n\n|\*\*争议)", bg_section, re.DOTALL)
    if bg_match:
        paras = [bg_match.group(1).strip()]
    
    issue_stmt = title
    
    background = {
        "issue_statement": issue_stmt,
        "paragraphs": paras,
        "why_this_issue": bg_why.strip() if bg_why else "",
        "framings": framings or [{"en_term": "Government responsibility", "cn_explanation": "从建筑法规执行缺失切入，论证治理失职"}],
    }
    
    # --- Passage ---
    psg_sec = sections.get("外刊核心段落", "")
    psg_source = ""
    psg_full = ""
    arg_labels = []
    reading_guide = ""
    
    # Extract source
    src_m = re.search(r"\*\*来源[：:]\*\*\s*(.+?)(?:\n|$)", psg_sec)
    if src_m:
        psg_source = src_m.group(1).strip()
    
    # Extract reading guide
    guide_m = re.search(r"\*\*阅读指引[：:]\*\*\s*(.+?)(?:\n\n|$)", psg_sec, re.DOTALL)
    if guide_m:
        reading_guide = guide_m.group(1).strip()
    
    # Extract argument paragraphs with labels
    label_patterns = [
        (r"\*\*Thesis\*\*\s+(.+?)(?=\*\*(?:Thesis|Premise|Evidence|Counter|Conclusion|[^\*])|$)", "thesis", "Thesis"),
        (r"\*\*Premise\s*\d*\*\*\s+(.+?)(?=\*\*(?:Thesis|Premise|Evidence|Counter|Conclusion|[^\*])|$)", "premise", "Premise"),
        (r"\*\*Evidence\*\*\s+(.+?)(?=\*\*(?:Thesis|Premise|Evidence|Counter|Conclusion|[^\*])|$)", "evidence", "Evidence"),
        (r"\*\*Counter-argument\*\*\s+(.+?)(?=\*\*(?:Thesis|Premise|Evidence|Counter|Conclusion|[^\*])|$)", "counter_argument", "Counter-arg"),
        (r"\*\*Conclusion\*\*\s+(.+?)(?=\*\*(?:Thesis|Premise|Evidence|Counter|Conclusion|[^\*])|$)", "conclusion", "Conclusion"),
    ]
    
    # Build full text + labels
    paragraphs = []
    cursor = 0
    for pat, label_type, label_cn in label_patterns:
        m = re.search(pat, psg_sec, re.DOTALL)
        if m:
            text = m.group(1).strip()
            # Remove trailing [Label]
            text = re.sub(r"\s*\[.*?\]\s*$", "", text)
            paragraphs.append(text)
            
            # Find position in joined text
            pos = len("\n\n".join(paragraphs[:-1]))
            arg_labels.append({
                "start": pos if pos < len(text) else 0,
                "end": pos + len(text),
                "type": label_type,
                "label_cn": label_cn,
            })
    
    # For simplicity, use first paragraph as full text with labels
    if paragraphs:
        psg_full = "\n\n".join(paragraphs)
        # Recalculate label positions correctly
        arg_labels = []
        offset = 0
        for i, p in enumerate(paragraphs):
            for pat, label_type, label_cn in label_patterns:
                m = re.search(pat, psg_sec, re.DOTALL)
                if m and m.group(1).strip() == p:
                    arg_labels.append({
                        "start": offset,
                        "end": offset + len(p),
                        "type": label_type,
                        "label_cn": label_cn,
                    })
                    break
            offset += len(p) + 2  # +2 for \n\n
    
    passage = {
        "source_title": "The Guardian / BBC",
        "source_name": "World News",
        "source_date": date_iso,
        "adapted_for_training": True,
        "full_text": psg_full or psg_sec[:500],
        "argument_labels": arg_labels or [
            {"start": 0, "end": len(psg_full) if psg_full else 100, "type": "thesis", "label_cn": "Thesis"}
        ],
        "reading_guide": reading_guide,
    }
    
    # --- Expressions ---
    expr_sec = sections.get("5个可迁移表达", "")
    expressions = []
    
    # Match ### 表达 N — category
    expr_blocks = re.split(r"###\s*表达\s*(\d+)\s*[——\-]\s*(.+)", expr_sec)
    # expr_blocks: [before, num1, cat1, content1, num2, cat2, content2, ...]
    for i in range(1, len(expr_blocks) - 1, 3):
        num = expr_blocks[i].strip()
        cat = expr_blocks[i+1].strip()
        content = expr_blocks[i+2] if i+2 < len(expr_blocks) else ""
        
        phrase = ""
        fn_tag = cat
        register = ""
        cn_exp = ""
        collocations = []
        example = ""
        
        for line in content.split("\n"):
            s = line.strip()
            if s.startswith("**英文表达：**"):
                phrase = re.sub(r"\*\*英文表达[：:]\*\*\s*`?", "", s).strip().rstrip("`")
            elif s.startswith("**功能标签：**"):
                fn_tag = re.sub(r"\*\*功能标签[：:]\*\*\s*", "", s).strip()
            elif s.startswith("**语域标签：**"):
                register = re.sub(r"\*\*语域标签[：:]\*\*\s*", "", s).strip()
            elif s.startswith("**中文释义：**"):
                cn_exp = re.sub(r"\*\*中文释义[：:]\*\*\s*", "", s).strip()
            elif s.startswith("**常见搭配：**"):
                continue  # handled below
            elif s.startswith("**外刊例句：**"):
                example = re.sub(r"\*\*外刊例句[：:]\*\*\s*", "", s).strip()
            elif s.startswith("- `"):
                c = re.sub(r"^- `(.+)`$", r"\1", s).strip()
                if c:
                    collocations.append(c)
        
        expressions.append({
            "index": int(num),
            "phrase": phrase or "N/A",
            "function_tag": fn_tag or "N/A",
            "register": register or "formal",
            "sub_tag": "argument",
            "grammar_tag": "",
            "cn_explanation": cn_exp or "N/A",
            "collocations": collocations or ["N/A"],
            "example_sentence": example or "N/A",
        })
    
    if not expressions:
        expressions = [{"index": 1, "phrase": "N/A", "function_tag": "N/A", "register": "formal", "sub_tag": "N/A", "grammar_tag": "N/A", "cn_explanation": "N/A", "collocations": ["N/A"], "example_sentence": "N/A"}]
    
    # --- Sentence Deconstruction ---
    sd_sec = sections.get("高级句型拆解", "")
    
    target = ""
    structure_analysis = ""
    structure_template = ""
    grammar_points = []
    imitation_template = ""
    applicable_scenarios = ""
    reference_imitation = ""
    
    tm = re.search(r"\*\*目标句[：:]\*\*\s*\n?\s*>\s*\*?(.+?)(?=\*\*结构分析|\*\*语法|$)", sd_sec, re.DOTALL)
    if tm: target = tm.group(1).strip().lstrip("*").strip()
    
    sam = re.search(r"\*\*结构分析[：:]\*\*\s*(.+?)(?=\*\*语法|$)", sd_sec, re.DOTALL)
    if sam: structure_analysis = sam.group(1).strip()
    
    stm = re.search(r"\*\*模板句型[：:]\*\*\s*\n```\n(.+?)\n```", sd_sec, re.DOTALL)
    if stm: structure_template = stm.group(1).strip()
    
    # Grammar points: **语法点 N — title** followed by explanation
    for gp_m in re.finditer(r"\*\*语法点\s*(\d+)\s*[——\-]\s*(.+?)\*\*[：:]\s*(.+?)(?=\*\*语法点|\*\*仿写|\*\*适用|$)", sd_sec, re.DOTALL):
        grammar_points.append({
            "title": gp_m.group(2).strip(),
            "explanation": gp_m.group(3).strip(),
        })
    
    im_m = re.search(r"\*\*仿写练习[：:]\*\*\s*\n?(.+?)(?=\*\*适用场景|\*\*仿写模板|$)", sd_sec, re.DOTALL)
    if im_m: imitation_template = im_m.group(1).strip()[:500]
    
    as_m = re.search(r"\*\*适用场景[：:]\*\*\s*\n?(.+?)(?=$|\*\*)", sd_sec, re.DOTALL)
    if as_m: applicable_scenarios = as_m.group(1).strip()
    
    sentence_deconstruction = {
        "target_sentence": target[:500],
        "structure_analysis": structure_analysis[:1000],
        "structure_template": structure_template[:500],
        "grammar_points": grammar_points or [{"title": "Core structure", "explanation": "See analysis above."}],
        "imitation_template": imitation_template[:500],
        "applicable_scenarios": applicable_scenarios[:500],
        "reference_imitation": "",
    }
    
    # --- Argument Chain ---
    ac_sec = sections.get("中文观点 → 英文 Argument Chain", "")
    
    cn_viewpoint = ""
    en_core = ""
    causal_chain = []
    weighing = []
    sample_paragraph = ""
    
    cvm = re.search(r"\*\*中文观点[：:]\*\*\s*(.+?)(?=\*\*英文核心|\*\*因果链|$)", ac_sec, re.DOTALL)
    if cvm: cn_viewpoint = cvm.group(1).strip()
    
    ecm = re.search(r"\*\*英文核心句[：:]\*\*\s*(.+?)(?=\*\*因果链|\*\*权衡|$)", ac_sec, re.DOTALL)
    if ecm: en_core = ecm.group(1).strip()
    
    # Causal chain steps
    for step_m in re.finditer(r"^\d+\.\s+(.+?)(?=\n\d+\.|\n\*\*权衡|\n\*\*范文|$)", ac_sec, re.DOTALL | re.MULTILINE):
        causal_chain.append({"step": step_m.group(1).strip()[:150], "explanation": ""})
    
    # Weighing: counter + rebuttal pairs
    counter_blocks = re.findall(r"反方[：:]\s*(.+?)(?=正方[：:]|结论[：:]|$)", ac_sec, re.DOTALL)
    rebuttal_blocks = re.findall(r"正方[：:]\s*(.+?)(?=结论[：:]|$)", ac_sec, re.DOTALL)
    for i in range(max(len(counter_blocks), len(rebuttal_blocks))):
        weighing.append({
            "counter_argument": counter_blocks[i].strip()[:300] if i < len(counter_blocks) else "",
            "your_rebuttal": rebuttal_blocks[i].strip()[:300] if i < len(rebuttal_blocks) else "",
        })
    
    spm = re.search(r"\*\*范文段落.+?\*\*\s*\n\s*>\s+(.+?)(?=\n\n|\*\*|\Z)", ac_sec, re.DOTALL)
    if spm: sample_paragraph = spm.group(1).strip()
    
    argument_chain = {
        "cn_viewpoint": cn_viewpoint[:500],
        "en_core_concept": en_core[:500],
        "causal_chain": causal_chain or [{"step": "Analysis pending", "explanation": ""}],
        "weighing": weighing or [{"counter_argument": "", "your_rebuttal": ""}],
        "sample_paragraph": sample_paragraph[:1000],
        "expression_count_used": 4,
    }
    
    # --- Output Tasks ---
    ot_sec = sections.get("输出任务", "")
    
    writing_task = ""
    speaking_task = ""
    structure_guide = []
    self_check = []
    
    # Task A: Writing
    wt_m = re.search(r"###\s*Task\s*A.+?\n\n\*\*题目[：:]\*\*\s*\*?(.+?)(?=\*\*结构指南|\*\*自测清单|###\s*Task\s*B|$)", ot_sec, re.DOTALL)
    if wt_m: writing_task = wt_m.group(1).strip()
    
    # Task B: Speaking
    st_m = re.search(r"###\s*Task\s*B.+?\n\n\*\*题目[：:]\*\*\s*\*?(.+?)(?=\*\*思维拓展|\*\*结构指南|\*\*自测清单|###|$)", ot_sec, re.DOTALL)
    if st_m: speaking_task = st_m.group(1).strip()
    
    # Structure guide steps
    step_start = 0
    for step_m in re.finditer(r"(?:STEP|步骤)\s*(\d+|A|B|C|D|一|二|三|四)[：:．.\s]+(.+?)(?=(?:STEP|步骤)\s*(\d+|A|B|C|D|一|二|三|四)|###|$)", ot_sec, re.DOTALL):
        structure_guide.append({"step": step_m.group(1), "guide": step_m.group(2).strip()[:200]})
    
    # Also try numbered steps (1. / 2. / etc.)
    if not structure_guide:
        for step_m in re.finditer(r"^\d+\.\s+\*\*(.+?)\*\*[：:]\s*(.+?)(?=\n\d+\.|\n*\*\*自测|\n-+\s*$|$)", ot_sec, re.MULTILINE | re.DOTALL):
            structure_guide.append({"step": step_m.group(1), "guide": step_m.group(2).strip()[:200]})
    
    # Self-check items
    in_check = False
    for line in ot_sec.split("\n"):
        s = line.strip()
        if "自测清单" in s or "Self-Check" in s:
            in_check = True; continue
        if in_check and s.startswith("- [ ]"):
            self_check.append(s[5:].strip())
        elif in_check and s.startswith("- ["):
            self_check.append(s[3:].strip())
        elif in_check and not s:
            continue
        elif in_check and s.startswith("---"):
            in_check = False
    
    output_tasks = {
        "writing_task": writing_task[:300] or "See briefing",
        "writing_word_count": "280–320 词",
        "speaking_task": speaking_task[:300] or "See briefing",
        "speaking_duration": "1.5–2 分钟",
        "structure_guide": structure_guide or [{"step": "1", "guide": "See briefing structure guide."}],
        "self_check": self_check or ["See briefing checklist."],
    }
    
    # --- Assemble ---
    content_json = {
        "header": header,
        "background": background,
        "passage": passage,
        "expressions": expressions,
        "sentence_deconstruction": sentence_deconstruction,
        "argument_chain": argument_chain,
        "output_tasks": output_tasks,
        "footer": {
            "brand": "ArgueLab",
            "generated": f"generated {date_iso}",
            "slogan": "Read like a scholar. Argue like a native.",
        },
        "raw_markdown": text,
    }
    
    return content_json


def main():
    if len(sys.argv) < 2:
        print("Usage: python quick_ingest.py <briefing.md>")
        sys.exit(1)
    
    md_path = sys.argv[1]
    print(f"Parsing: {md_path}")
    content_json = parse_briefing(md_path)
    
    slug = content_json["header"]["slug"]
    print(f"Slug: {slug}")
    print(f"Title: {content_json['header']['title'][:60]}")
    print(f"Expressions: {len(content_json['expressions'])}")
    print(f"Arg labels: {len(content_json['passage']['argument_labels'])}")
    print(f"Causal chain: {len(content_json['argument_chain']['causal_chain'])}")
    print(f"Self-check: {len(content_json['output_tasks']['self_check'])}")
    
    # POST to Railway
    print(f"\nPosting to Railway...")
    body = {"content_json": content_json, "status": "published"}
    
    resp = requests.put(
        f"{RAILWAY_API}/{slug}",
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    
    data = resp.json()
    if resp.status_code in (200, 201):
        print(f"✓ Updated! Status: {data.get('status', 'ok')}")
        print(f"  Web: https://arguelab-production.up.railway.app/issues/{slug}")
    else:
        print(f"✗ Error ({resp.status_code}): {data}")
        # Try POST as fallback
        print("Trying POST instead...")
        resp2 = requests.post(
            RAILWAY_API,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        data2 = resp2.json()
        if resp2.status_code in (200, 201):
            print(f"✓ Created! {data2.get('slug', '')}")
        else:
            print(f"✗ POST also failed ({resp2.status_code}): {data2}")


if __name__ == "__main__":
    main()
