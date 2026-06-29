#!/usr/bin/env node
/**
 * render-pdf.js — Render an ArgueLab briefing markdown file to PDF.
 *
 * Usage:
 *   node render-pdf.js <briefing.md> <output.pdf>
 *
 * Produces an academic briefing handout PDF with:
 * - White background, Songti/Georgia typography
 * - No Markdown residue (#, ###, *, >, bare backticks)
 * - Structured sections: Header, Context, Passage, Expressions,
 *   Sentence Deconstruction, Argument Chain, Output Tasks
 * - Notes Boxes, Cornell-style Notes, My Argument Draft
 * - Print-friendly page breaks, 8–11 pages target
 */

const path = require('path');
const fs = require('fs');

// ── Load puppeteer ──
let puppeteer = null;
const tryPaths = [
  path.join(__dirname, 'node_modules', 'puppeteer-core'),
  path.join(__dirname, 'node_modules', 'puppeteer'),
  'puppeteer-core',
  'puppeteer',
];

for (const p of tryPaths) {
  try { puppeteer = require(p); console.error('[pdf] Loaded puppeteer from:', p); break; }
  catch (_) {}
}

if (!puppeteer) {
  console.error('[pdf] ERROR: Puppeteer not found.\n' +
    'Install it: cd subscription-product/scripts && npm install puppeteer-core');
  process.exit(1);
}

// ═══════════════════════════════════════════════════════════════════════
// PRINT CSS — Academic Briefing Handout Style
// White background, Songti/Georgia, muted functional colors, printable
// ═══════════════════════════════════════════════════════════════════════

// ── Find Chrome/Chromium executable ──
function findChromium() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  if (process.env.CHROME_PATH)   return process.env.CHROME_PATH;
  const fs = require('fs');
  const macPaths = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
  ];
  for (const p of macPaths) { if (fs.existsSync(p)) return p; }
  const linuxPaths = [
    '/usr/bin/chromium', '/usr/bin/chromium-browser',
    '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
    '/snap/bin/chromium',
  ];
  for (const p of linuxPaths) { if (fs.existsSync(p)) return p; }
  return undefined;
}

const PRINT_CSS = `
  /* ── CJK Font Face ── */
  /* Mac: Songti SC (serif), PingFang SC (sans); Linux: WenQuanYi; Fallback: Noto Sans CJK */
  @font-face {
    font-family: 'ArgueLab CJK';
    src: local('Songti SC'),
         local('Songti TC'),
         local('STSong'),
         local('SimSun'),
         local('PingFang SC'),
         local('PingFang TC'),
         local('Heiti SC'),
         local('WenQuanYi Zen Hei'),
         local('WenQuanYiZhenHei'),
         local('Noto Sans CJK SC'),
         local('Noto Serif CJK SC'),
         local('Microsoft YaHei');
    font-weight: normal;
    font-style: normal;
  }

  @font-face {
    font-family: 'ArgueLab CJK';
    src: local('Songti SC'),
         local('STSong'),
         local('PingFang SC'),
         local('WenQuanYi Zen Hei'),
         local('Noto Serif CJK SC');
    font-weight: bold;
    font-style: normal;
  }

  /* ── Page setup ── */
  @page {
    size: A4;
    margin: 22mm 24mm 20mm 24mm;
    @bottom-center {
      content: "— " counter(page) " —";
      font-size: 9pt;
      color: #999;
      font-family: Georgia, 'Times New Roman', serif;
    }
  }

  /* ── CSS Variables ── */
  :root {
    --bg: #fff;
    --surface: #fafafa;
    --ink: #1a1a1a;
    --ink-dim: #444;
    --ink-muted: #777;
    --ink-faint: #aaa;

    /* Functional colors — muted, low-saturation, print-safe */
    --clr-context: #4a6fa5;
    --clr-passage: #3d6a9e;
    --clr-expression: #a67c2e;
    --clr-sentence: #9e5670;
    --clr-chain: #3a7d6a;
    --clr-output: #9e7e3e;
    --clr-notes: #7a8a9e;
    --clr-cornell: #6b7d8e;

    /* Argument label colors */
    --arg-thesis: #b8860b;
    --arg-premise: #2e8b57;
    --arg-evidence: #4682b4;
    --arg-counter: #b22222;
    --arg-conclusion: #6a5acd;

    --border: rgba(0,0,0,0.08);
    --divider: rgba(0,0,0,0.06);
    --code-bg: #f4f4f4;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: Georgia, 'Times New Roman', 'ArgueLab CJK', 'Songti SC', 'Noto Serif CJK SC', 'Charter',
                 'Songti SC', 'SimSun', 'Source Han Serif SC', serif;
    font-size: 11pt;
    line-height: 1.65;
    color: var(--ink);
    background: #fff;
  }

  /* ── Chinese text uses serif CJK ── */
  .cn-body, .ctx-text, .expr-cn, .step-cn, .weigh-text {
    font-family: 'WenQuanYi Zen Hei', 'Noto Serif CJK SC', 'Songti SC', 'SimSun',
                 'Source Han Serif SC', 'Noto Serif CJK TC',
                 'PingFang SC', 'Microsoft YaHei', sans-serif;
    font-size: 10.5pt;
    line-height: 1.7;
    color: var(--ink-dim);
  }

  /* ── English body ── */
  .en-body, .ctx-text-en, .ts-text, .sp-text {
    font-family: Georgia, 'Times New Roman', 'Charter', serif;
    font-size: 11pt;
    line-height: 1.6;
    color: var(--ink);
  }

  /* ── Code / Template ── */
  code, pre, .tpl-code, .gm-code, .step-code, .template-box {
    font-family: 'SF Mono', 'Menlo', 'Consolas', 'Monaco', monospace;
    font-size: 9pt;
    line-height: 1.55;
    background: var(--code-bg);
    color: var(--ink-dim);
    white-space: pre-wrap;
    word-break: break-word;
    overflow-wrap: break-word;
  }
  code { padding: 1px 4px; border-radius: 2px; font-size: 9.5pt; }
  pre, .tpl-code, .gm-code, .step-code, .template-box {
    padding: 8px 12px;
    border-radius: 4px;
    margin: 6px 0;
  }

  /* ── Page wrapper ── */
  .page-wrapper { max-width: 100%; padding: 0; }
  .page { max-width: 100%; }

  /* ═══════════════════════════════════════════════════════════════════
     HEADER
     ═══════════════════════════════════════════════════════════════════ */
  .pdf-header {
    padding: 14px 0 18px;
    text-align: center;
    border-bottom: 1.5px solid var(--border);
    margin-bottom: 22px;
    page-break-after: avoid;
  }
  .pdf-header .brand {
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 20pt;
    font-weight: 700;
    letter-spacing: 1px;
    margin-bottom: 4px;
  }
  .pdf-header .brand span { color: var(--clr-passage); }
  .pdf-header .subtitle {
    font-size: 9pt;
    color: var(--ink-muted);
    font-style: italic;
    margin-bottom: 6px;
    letter-spacing: 0.5px;
  }
  .pdf-header .meta-line {
    font-size: 9.5pt;
    color: var(--ink-dim);
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .pdf-header .meta-line .meta-sep { color: var(--ink-faint); margin: 0 6px; }
  .pdf-header .slug {
    font-size: 8pt;
    letter-spacing: 2px;
    color: var(--clr-passage);
    text-transform: uppercase;
    margin-bottom: 8px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }

  /* ═══════════════════════════════════════════════════════════════════
     SECTION TITLES
     ═══════════════════════════════════════════════════════════════════ */
  .issue-section { margin-bottom: 20px; }
  .section-title {
    font-size: 15pt;
    font-weight: 700;
    margin-bottom: 10pt;
    padding-bottom: 6pt;
    border-bottom: 1px solid var(--divider);
    page-break-after: avoid;
    break-after: avoid;
  }
  .section-title .sec-num {
    display: inline-block;
    font-size: 10pt;
    font-weight: 600;
    margin-right: 8px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
    letter-spacing: 0.5px;
  }
  .section-title .sec-num-context { color: var(--clr-context); }
  .section-title .sec-num-passage { color: var(--clr-passage); }
  .section-title .sec-num-expression { color: var(--clr-expression); }
  .section-title .sec-num-sentence { color: var(--clr-sentence); }
  .section-title .sec-num-chain { color: var(--clr-chain); }
  .section-title .sec-num-output { color: var(--clr-output); }

  /* ── Sub-headings ── */
  .sub-heading {
    font-size: 11.5pt;
    font-weight: 700;
    margin-top: 10pt;
    margin-bottom: 5pt;
    page-break-after: avoid;
    break-after: avoid;
  }
  .sub-heading-context { color: var(--clr-context); }
  .sub-heading-passage { color: var(--clr-passage); }
  .sub-heading-expression { color: var(--clr-expression); }
  .sub-heading-sentence { color: var(--clr-sentence); }
  .sub-heading-chain { color: var(--clr-chain); }
  .sub-heading-output { color: var(--clr-output); }

  /* ── Label tags ── */
  .label-tag {
    display: inline-block;
    font-size: 8.5pt;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .label-tag-context { color: var(--clr-context); }
  .label-tag-passage { color: var(--clr-passage); }
  .label-tag-expression { color: var(--clr-expression); }
  .label-tag-sentence { color: var(--clr-sentence); }
  .label-tag-chain { color: var(--clr-chain); }
  .label-tag-output { color: var(--clr-output); }

  /* ═══════════════════════════════════════════════════════════════════
     SECTION 1: CONTEXT (背景)
     ═══════════════════════════════════════════════════════════════════ */
  .ctx-block {
    margin-bottom: 10pt;
    padding-left: 0;
  }
  .ctx-label {
    font-size: 9pt;
    font-weight: 700;
    color: var(--clr-context);
    margin-bottom: 3pt;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .ctx-text, .ctx-text-en { margin-bottom: 6pt; }
  .ctx-list {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .ctx-list li {
    font-size: 10pt;
    color: var(--ink-dim);
    padding: 2pt 0 2pt 16pt;
    position: relative;
    line-height: 1.6;
  }
  .ctx-list li::before {
    content: '▸';
    position: absolute;
    left: 0;
    color: var(--clr-context);
    font-size: 8pt;
    top: 3pt;
  }

  /* ═══════════════════════════════════════════════════════════════════
     SECTION 2: PASSAGE (外刊段落)
     ═══════════════════════════════════════════════════════════════════ */
  .source-line {
    font-size: 9pt;
    color: var(--ink-muted);
    font-style: italic;
    margin-bottom: 10pt;
  }

  .passage-block {
    background: var(--surface);
    border-left: 3px solid var(--clr-passage);
    padding: 14pt 18pt;
    margin-bottom: 10pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .passage-block p {
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 11pt;
    line-height: 1.75;
    color: var(--ink);
  }

  /* Argument labels */
  .arg-label {
    display: inline-block;
    font-size: 8pt;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 2px;
    margin-right: 2px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    vertical-align: middle;
  }
  .arg-thesis { background: rgba(184,134,11,0.1); color: var(--arg-thesis); }
  .arg-premise { background: rgba(46,139,87,0.1); color: var(--arg-premise); }
  .arg-evidence { background: rgba(70,130,180,0.1); color: var(--arg-evidence); }
  .arg-counter { background: rgba(178,34,34,0.1); color: var(--arg-counter); }
  .arg-conclusion { background: rgba(106,90,205,0.1); color: var(--arg-conclusion); }

  .guide-block {
    background: rgba(61,106,158,0.04);
    border-left: 3px solid var(--clr-passage);
    padding: 10pt 14pt;
    font-size: 10pt;
    color: var(--ink-dim);
    line-height: 1.6;
    page-break-inside: avoid;
    break-inside: avoid;
  }

  /* ═══════════════════════════════════════════════════════════════════
     SECTION 3: EXPRESSIONS (可迁移表达)
     ═══════════════════════════════════════════════════════════════════ */
  .expr-item {
    border-left: 2.5px solid var(--clr-expression);
    padding: 10pt 14pt;
    margin-bottom: 10pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .expr-num {
    font-size: 9pt;
    font-weight: 700;
    color: var(--clr-expression);
    margin-bottom: 2pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .expr-phrase {
    font-family: 'SF Mono', 'Menlo', 'Consolas', monospace;
    font-size: 11pt;
    font-weight: 600;
    color: var(--ink);
    margin-bottom: 4pt;
  }
  .expr-tags {
    font-size: 8.5pt;
    color: var(--clr-expression);
    margin-bottom: 6pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
    letter-spacing: 0.3px;
  }
  .expr-cn {
    font-size: 10pt;
    margin-bottom: 5pt;
  }
  .expr-colloc {
    font-size: 9.5pt;
    color: var(--ink-dim);
    margin-bottom: 3pt;
  }
  .expr-colloc strong {
    color: var(--ink);
    font-size: 9pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
    font-weight: 600;
  }
  .expr-example {
    font-size: 10pt;
    font-style: italic;
    color: var(--ink-dim);
    margin-top: 6pt;
    padding-top: 6pt;
    border-top: 1px solid var(--divider);
    line-height: 1.55;
  }

  /* ═══════════════════════════════════════════════════════════════════
     SECTION 4: SENTENCE DECONSTRUCTION (句型拆解)
     ═══════════════════════════════════════════════════════════════════ */
  .target-sentence-block {
    background: var(--surface);
    border-left: 3px solid var(--clr-sentence);
    padding: 12pt 16pt;
    margin-bottom: 10pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .ts-label {
    font-size: 9pt;
    font-weight: 600;
    color: var(--clr-sentence);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 6pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .ts-text {
    font-style: italic;
  }

  .why-block {
    margin-bottom: 10pt;
    padding: 0 4pt;
  }
  .why-label {
    font-size: 9pt;
    font-weight: 600;
    color: var(--clr-sentence);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 4pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .why-text { font-size: 10.5pt; color: var(--ink-dim); line-height: 1.65; }

  .grammar-section { margin-bottom: 10pt; }
  .grammar-section-label {
    font-size: 9pt;
    font-weight: 600;
    color: var(--clr-sentence);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 6pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .grammar-mini-card {
    border-left: 2px solid rgba(158,86,112,0.3);
    padding: 8pt 12pt;
    margin-bottom: 6pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .gm-title {
    font-size: 10pt;
    font-weight: 700;
    margin-bottom: 3pt;
  }
  .gm-body { font-size: 9.5pt; color: var(--ink-dim); line-height: 1.55; }

  .template-block {
    border-left: 3px solid var(--clr-sentence);
    padding: 10pt 14pt;
    margin-bottom: 10pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .tpl-label {
    font-size: 9pt;
    font-weight: 600;
    color: var(--clr-sentence);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 6pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }

  .imitation-block {
    padding: 0 4pt;
    margin-bottom: 10pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .imit-label {
    font-size: 9pt;
    font-weight: 600;
    color: var(--clr-sentence);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 4pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .imit-text { font-size: 10pt; font-style: italic; color: var(--ink-dim); line-height: 1.55; }

  .scenario-tag {
    display: inline-block;
    font-size: 8.5pt;
    color: var(--clr-sentence);
    background: rgba(158,86,112,0.06);
    padding: 3pt 8pt;
    border-radius: 3px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
    margin-top: 4pt;
  }

  /* ═══════════════════════════════════════════════════════════════════
     SECTION 5: ARGUMENT CHAIN
     ═══════════════════════════════════════════════════════════════════ */
  .chain-flow { display: flex; flex-direction: column; gap: 6pt; }
  .chain-step {
    border-left: 2.5px solid var(--clr-chain);
    padding: 10pt 14pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .step-label {
    font-size: 9pt;
    font-weight: 700;
    color: var(--clr-chain);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 4pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .step-en { font-size: 11pt; font-weight: 600; color: var(--ink); line-height: 1.55; }
  .step-cn { font-size: 10pt; margin-top: 3pt; }

  .weighing-block {
    border-left: 2.5px solid var(--clr-chain);
    padding: 12pt 16pt;
    margin-top: 6pt;
  }
  .weigh-label {
    font-size: 9pt;
    font-weight: 700;
    color: var(--clr-chain);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 6pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .weigh-text { font-size: 10.5pt; line-height: 1.75; }
  .weigh-text p { margin-bottom: 8pt; }

  .sample-para-block {
    background: var(--surface);
    border-left: 3px solid var(--clr-chain);
    padding: 12pt 16pt;
    margin-top: 6pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .sp-label {
    font-size: 9pt;
    font-weight: 700;
    color: var(--clr-chain);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 6pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .sp-note {
    font-size: 9pt;
    color: var(--ink-muted);
    margin-top: 8pt;
    padding-top: 6pt;
    border-top: 1px solid var(--divider);
    font-style: italic;
  }

  /* ═══════════════════════════════════════════════════════════════════
     SECTION 6: OUTPUT TASKS
     ═══════════════════════════════════════════════════════════════════ */
  .task-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12pt;
    margin-bottom: 10pt;
  }
  .task-card {
    border-top: 2.5px solid var(--clr-output);
    padding: 12pt 14pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .task-type {
    font-size: 9pt;
    font-weight: 700;
    color: var(--clr-output);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 6pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .task-prompt { font-size: 10.5pt; line-height: 1.6; }
  .task-meta {
    font-size: 8.5pt;
    color: var(--ink-muted);
    margin-top: 4pt;
    font-style: italic;
  }

  .guide-card {
    border-left: 2.5px solid var(--clr-output);
    padding: 10pt 14pt;
    margin-bottom: 10pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .guide-label {
    font-size: 9pt;
    font-weight: 700;
    color: var(--clr-output);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 8pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .structure-guide {
    margin-bottom: 10pt;
  }
  .step-item {
    display: grid;
    grid-template-columns: 18pt 1fr;
    gap: 8pt;
    padding: 6pt 0;
    border-bottom: 0.5px solid #ece8df;
    break-inside: avoid;
    page-break-inside: avoid;
  }
  .step-item:last-child { border-bottom: none; }
  .step-number {
    width: 16pt;
    height: 16pt;
    border-radius: 4px;
    background: #f3ead2;
    color: #8a6a1f;
    font-size: 8pt;
    font-weight: 700;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .step-content {
    font-size: 10pt;
    color: var(--ink-dim);
    line-height: 1.55;
  }

  .check-card {
    border-left: 2.5px solid #4a7c80;
    padding: 10pt 14pt;
    margin-bottom: 10pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .check-label {
    font-size: 9pt;
    font-weight: 700;
    color: #4a7c80;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 8pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .self-check-list {
    margin-top: 8pt;
  }
  .check-item {
    display: flex;
    gap: 7pt;
    align-items: flex-start;
    padding: 5pt 0;
    border-bottom: 0.5px solid #ece8df;
    break-inside: avoid;
    page-break-inside: avoid;
  }
  .check-item:last-child { border-bottom: none; }
  .check-box {
    width: 9pt;
    height: 9pt;
    border: 1px solid #b8b2a8;
    border-radius: 2px;
    flex: 0 0 auto;
    margin-top: 2pt;
  }
  .check-text {
    flex: 1;
    font-size: 10pt;
    color: var(--ink-dim);
    line-height: 1.55;
  }

  /* ── Task Block (two-task layout) ── */
  .task-block {
    margin-bottom: 24pt;
    padding: 16pt 18pt;
    border: 1.5px solid var(--divider);
    border-radius: 6px;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .task-header {
    display: flex;
    align-items: center;
    gap: 8pt;
    margin-bottom: 12pt;
    padding-bottom: 8pt;
    border-bottom: 1px solid var(--divider);
  }
  .task-header .task-type {
    font-size: 9pt;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: #9e7e3e;
  }
  .task-header .task-meta {
    font-size: 8.5pt;
    color: #999;
    font-family: "PingFang SC", "Songti SC", "SimSun", serif;
  }
  .task-block .task-prompt {
    font-size: 10.5pt;
    line-height: 1.6;
    color: #333;
    padding: 10pt 14pt;
    background: #fafafa;
    border-left: 2.5px solid #9e7e3e;
    margin-bottom: 14pt;
    font-style: italic;
  }
  .task-block .guide-card,
  .task-block .check-card {
    margin-top: 10pt;
  }

  /* ── Premium Hint Card (PDF print) ── */
  .premium-hint-card {
    margin-top: 20pt;
    padding: 14pt 18pt;
    border: 1.5px solid rgba(180,150,60,0.25);
    border-radius: 6px;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .ph-label {
    font-size: 9pt;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: #a08030;
    margin-bottom: 10pt;
  }
  .ph-text {
    font-size: 10pt;
    line-height: 1.6;
    color: #555;
  }

  /* ═══════════════════════════════════════════════════════════════════
     NOTES BOX — After each major section
     ═══════════════════════════════════════════════════════════════════ */
  .notes-box {
    border: 1px solid rgba(0,0,0,0.1);
    padding: 0;
    margin-top: 14pt;
    margin-bottom: 8pt;
    page-break-inside: avoid;
    break-inside: avoid;
    min-height: 32mm;
    max-height: 45mm;
    display: flex;
    flex-direction: column;
  }
  .notes-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    padding: 6pt 10pt;
    border-bottom: 1px solid rgba(0,0,0,0.06);
    font-size: 8.5pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .notes-header .notes-title {
    font-weight: 600;
    color: var(--clr-notes);
    text-transform: uppercase;
    letter-spacing: 0.8px;
  }
  .notes-header .notes-hint {
    color: var(--ink-faint);
    font-style: italic;
  }
  .notes-body {
    flex: 1;
    min-height: 24mm;
  }

  /* ═══════════════════════════════════════════════════════════════════
     CORNELL NOTES — For sentence decon, argument chain, output tasks
     ═══════════════════════════════════════════════════════════════════ */
  .cornell-notes {
    border: 1px solid rgba(0,0,0,0.1);
    margin-top: 14pt;
    margin-bottom: 8pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .cornell-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    padding: 6pt 10pt;
    border-bottom: 1px solid rgba(0,0,0,0.06);
    font-size: 8.5pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .cornell-header .cornell-title {
    font-weight: 600;
    color: var(--clr-cornell);
    text-transform: uppercase;
    letter-spacing: 0.8px;
  }
  .cornell-header .cornell-hint {
    color: var(--ink-faint);
    font-style: italic;
  }
  .cornell-main {
    display: flex;
    border-bottom: 1px solid rgba(0,0,0,0.06);
    min-height: 32mm;
  }
  .cornell-cue {
    flex: 0 0 30%;
    padding: 8pt 10pt;
    border-right: 1px solid rgba(0,0,0,0.06);
  }
  .cornell-note {
    flex: 1;
    padding: 8pt 10pt;
  }
  .cornell-label {
    font-size: 8pt;
    color: var(--ink-faint);
    text-transform: uppercase;
    letter-spacing: 0.6px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
    margin-bottom: 6pt;
  }
  .cornell-summary {
    padding: 8pt 10pt;
    min-height: 14mm;
  }

  /* ═══════════════════════════════════════════════════════════════════
     MY ARGUMENT DRAFT — End of output tasks
     ═══════════════════════════════════════════════════════════════════ */
  .draft-area {
    border: 1px solid rgba(0,0,0,0.1);
    margin-top: 14pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .draft-header {
    padding: 6pt 10pt;
    border-bottom: 1px solid rgba(0,0,0,0.06);
    font-size: 8.5pt;
    font-weight: 600;
    color: var(--clr-output);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .draft-main {
    display: flex;
    border-bottom: 1px solid rgba(0,0,0,0.06);
    min-height: 40mm;
  }
  .draft-outline {
    flex: 0 0 32%;
    padding: 10pt 12pt;
    border-right: 1px solid rgba(0,0,0,0.06);
  }
  .draft-outline .do-label {
    font-size: 8pt;
    color: var(--ink-faint);
    text-transform: uppercase;
    letter-spacing: 0.6px;
    margin-bottom: 8pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .draft-outline .do-item {
    font-size: 9pt;
    color: var(--ink-dim);
    padding: 2pt 0;
  }
  .draft-outline .do-item strong {
    font-size: 8pt;
    color: var(--ink-muted);
    font-weight: 600;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
    display: block;
    margin-bottom: 1pt;
  }
  .draft-writing {
    flex: 1;
    padding: 10pt 12pt;
  }
  .draft-writing .dw-label {
    font-size: 8pt;
    color: var(--ink-faint);
    text-transform: uppercase;
    letter-spacing: 0.6px;
    margin-bottom: 6pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }
  .draft-takeaway {
    padding: 10pt 12pt;
    min-height: 12mm;
  }
  .dt-label {
    font-size: 8pt;
    color: var(--ink-faint);
    text-transform: uppercase;
    letter-spacing: 0.6px;
    margin-bottom: 4pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }

  /* ═══════════════════════════════════════════════════════════════════
     FOOTER
     ═══════════════════════════════════════════════════════════════════ */
  .page-footer {
    padding: 18pt 0 0;
    text-align: center;
    border-top: 1px solid var(--divider);
    margin-top: 20pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .page-footer .brand {
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 14pt;
    font-weight: 700;
    margin-bottom: 2pt;
  }
  .page-footer .brand span { color: var(--clr-passage); }
  .page-footer .slogan {
    font-size: 9pt;
    color: var(--ink-muted);
    font-style: italic;
  }
  .page-footer .gen-date {
    font-size: 8pt;
    color: var(--ink-faint);
    margin-top: 3pt;
    font-family: system-ui, -apple-system, 'PingFang SC', 'ArgueLab CJK', sans-serif;
  }

  /* ═══════════════════════════════════════════════════════════════════
     PRINT / PAGE-BREAK RULES
     ═══════════════════════════════════════════════════════════════════ */
  h1, h2, h3 {
    break-after: avoid;
    page-break-after: avoid;
  }
  .section-title {
    break-after: avoid;
    page-break-after: avoid;
  }
  .issue-section { break-inside: auto; }
  .expr-item, .notes-box, .cornell-notes, .draft-area,
  .task-card, .template-block, .check-card, .guide-card,
  .passage-block, .guide-block, .target-sentence-block,
  .grammar-mini-card, .chain-step, .sample-para-block,
  .page-footer {
    break-inside: avoid;
    page-break-inside: avoid;
  }

  /* ── Typography helpers ── */
  p { margin-bottom: 7pt; }
  strong { font-weight: 700; }
  em { font-style: italic; }
  ul, ol { margin: 4pt 0; padding-left: 16pt; }
  li { font-size: 10pt; color: var(--ink-dim); padding: 2pt 0; line-height: 1.6; }

  /* ── Template / Code overflow safety ── */
  .template-block,
  .code-block,
  .tpl-code,
  .gm-code,
  .step-code {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
    line-height: 1.55;
  }

  /* ── Break-after rules: keep headings with their content ── */
  .notes-title,
  .self-check-title,
  .draft-title,
  .cornell-title {
    break-after: avoid;
    page-break-after: avoid;
  }

  /* ── Notes / Draft / Self-check: never split across pages ── */
  .notes-box,
  .cornell-notes,
  .draft-area,
  .self-check-section {
    break-inside: avoid;
    page-break-inside: avoid;
  }
`;

// ═══════════════════════════════════════════════════════════════════════
// UTILITY FUNCTIONS
// ═══════════════════════════════════════════════════════════════════════

function esc(text) {
  if (!text) return '';
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function mdInline(text) {
  if (!text) return '';
  // Apply markdown → HTML, THEN escape the result.
  // This ensures *bold* becomes <strong>bold</strong> without double-escaping issues.
  let html = text
    .replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(?<!\*)\*([^*\n]+?)\*(?!\*)/g, '<em>$1</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
  return html;
}

function stripMarkdown(text) {
  if (!text) return '';
  return text
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/(?<!\*)\*([^*\n]+?)\*(?!\*)/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/~~(.+?)~~/g, '$1')
    .trim();
}

function cleanText(text) {
  if (!text) return '';
  let t = text.replace(/^#{1,6}\s+/, '');
  t = t.replace(/^>\s?/, '');
  t = t.replace(/^[-*+]\s+/, '');
  return t;
}

function splitParagraphs(text) {
  return text.split(/\n{2,}/).map(p => p.trim()).filter(Boolean);
}

/**
 * Check if a value has meaningful content.
 * - null/undefined → false
 * - Array → true if ANY element has content (recursive)
 * - Object → true if ANY value has content (recursive)
 * - String → true if non-empty after trim
 */
function hasContent(value) {
  if (value == null) return false;
  if (Array.isArray(value)) return value.some(hasContent);
  if (typeof value === 'object') return Object.values(value).some(hasContent);
  return String(value).trim().length > 0;
}

// ═══════════════════════════════════════════════════════════════════════
// SECTION RENDERERS
// ═══════════════════════════════════════════════════════════════════════

// ── Section 1: Context (今日议题背景) ──
function renderContext(allText) {
  const html = [];
  const lines = allText.split('\n');

  let topic = '';
  let bgLines = [];
  let debateLines = [];
  let rationaleLines = [];
  let framingLines = [];

  let mode = 'intro';
  for (const line of lines) {
    const s = line.trim();
    if (!s) continue;

    // Sub-header detection — ordered by specificity
    if (s.startsWith('**背景：**') || s.startsWith('**背景**')) {
      // Background detected — capture any inline content, switch mode
      const inline = s.replace(/\*\*背景[：:]?\*\*\s*/, '');
      if (inline) bgLines.push(cleanText(inline));
      mode = 'background';
    } else if (s.startsWith('**争议：**') || s.startsWith('**争议**')) {
      const inline = s.replace(/\*\*争议[：:]?\*\*\s*/, '');
      if (inline) debateLines.push(cleanText(inline));
      mode = 'debate';
    } else if (s.startsWith('**为什么选这个议题：**') || s.startsWith('**为什么选这个议题？**') || s.startsWith('**为什么选这个议题**') || s.startsWith('**为什么选**')) {
      const inline = s.replace(/\*\*为什么选这个议题[：:？?]?\*\*\s*/, '').replace(/\*\*为什么选[：:]?\*\*\s*/, '');
      if (inline) rationaleLines.push(cleanText(inline));
      mode = 'rationale';
    } else if (s.startsWith('**Framing 提示：**') || s.startsWith('**Framing 提示**') || s.startsWith('**Framing**') || s.startsWith('**常见 framing') || s.startsWith('**常见 Framing')) {
      const inline = s.replace(/\*\*(?:Framing 提示|Framing|常见 framing 方式|常见 Framing)[：:]?\*\*\s*/, '');
      if (inline) framingLines.push(cleanText(inline));
      mode = 'framing';
    } else if (s.startsWith('**议题：**') || s.startsWith('**议题**')) {
      topic = s.replace(/\*\*议题[：:]?\*\*\s*/, '');
      mode = 'background'; // Background follows
    } else if (mode === 'intro' && s.startsWith('*') && !s.startsWith('**')) {
      // subtitle line, skip
    } else if (mode === 'intro') {
      topic = (topic ? topic + ' ' : '') + s;
    } else if (mode === 'background') {
      const isBullet = s.startsWith('- ');
      bgLines.push({ text: cleanText(s), isBullet });
    } else if (mode === 'debate') {
      const isBullet = s.startsWith('- ');
      debateLines.push({ text: cleanText(s), isBullet });
    } else if (mode === 'rationale') {
      rationaleLines.push(cleanText(s));
    } else if (mode === 'framing') {
      const isBullet = s.startsWith('- ');
      const cleaned = cleanText(s);
      if (cleaned) framingLines.push({ text: cleaned, isBullet });
    }
  }

  // Render topic
  if (topic) {
    html.push(`<div class="ctx-block"><div class="ctx-label">议题</div>` +
      `<div class="ctx-text">${mdInline(esc(cleanText(topic)))}</div></div>`);
  }

  // Render background
  if (bgLines.length > 0) {
    html.push(`<div class="ctx-block"><div class="ctx-label">背景</div>` +
      `<div class="ctx-text">${mdInline(esc(bgLines.map(l => l.text).join('\n')))}</div></div>`);
  }

  // Render debate — join lines, detect bullet items
  if (debateLines.length > 0) {
    const hasBullets = debateLines.some(l => l.isBullet);
    if (hasBullets) {
      let introParts = [];
      let bulletParts = [];
      for (const l of debateLines) {
        if (l.isBullet) {
          bulletParts.push(`<li>${mdInline(esc(l.text))}</li>`);
        } else {
          introParts.push(l.text);
        }
      }
      const introHtml = introParts.length > 0
        ? `<div class="ctx-text">${mdInline(esc(introParts.join(' ')))}</div>`
        : '';
      const listHtml = bulletParts.length > 0
        ? `<ul class="ctx-list">${bulletParts.join('')}</ul>`
        : '';
      html.push(`<div class="ctx-block"><div class="ctx-label">争议焦点</div>${introHtml}${listHtml}</div>`);
    } else {
      html.push(`<div class="ctx-block"><div class="ctx-label">争议焦点</div>` +
        `<div class="ctx-text">${mdInline(esc(debateLines.map(l => l.text).join('\n')))}</div></div>`);
    }
  }

  // Render rationale
  if (rationaleLines.length > 0) {
    html.push(`<div class="ctx-block"><div class="ctx-label">为什么选这个议题</div>` +
      `<div class="ctx-text">${mdInline(esc(rationaleLines.join(' ')))}</div></div>`);
  }

  // Render framing — detect bullet items for layered rendering
  if (framingLines.length > 0) {
    const hasBullets = framingLines.some(l => l.isBullet);
    if (hasBullets) {
      let introParts = [];
      let bulletParts = [];
      for (const l of framingLines) {
        if (l.isBullet) {
          bulletParts.push(`<li>${mdInline(esc(l.text))}</li>`);
        } else {
          introParts.push(l.text);
        }
      }
      const introHtml = introParts.length > 0
        ? `<div class="ctx-text">${mdInline(esc(introParts.join(' ')))}</div>`
        : '';
      const listHtml = bulletParts.length > 0
        ? `<ul class="ctx-list">${bulletParts.join('')}</ul>`
        : '';
      html.push(`<div class="ctx-block"><div class="ctx-label">Framing 提示</div>${introHtml}${listHtml}</div>`);
    } else {
      html.push(`<div class="ctx-block"><div class="ctx-label">Framing 提示</div>` +
        `<div class="ctx-text">${mdInline(esc(framingLines.map(l => l.text).join(' ')))}</div></div>`);
    }
  }

  return html.join('\n');
}

// ── Section 2: Passage (外刊核心段落) ──
function renderPassage(allText) {
  const LABEL_MAP = {
    Thesis: 'arg-thesis', Premise: 'arg-premise', Evidence: 'arg-evidence',
    'Counter-argument': 'arg-counter', Counterargument: 'arg-counter', Conclusion: 'arg-conclusion',
  };

  const lines = allText.split('\n');
  let sourceLine = '';
  let guideText = '';
  let inGuide = false;
  let passageBody = '';

  for (const line of lines) {
    const s = line.trim();
    if (!s) { inGuide = false; continue; }

    // Reading guide: starts with 📖 or Chinese reading guide, capture all subsequent lines until ---
    if (s.startsWith('📖') || s.startsWith('**📖')) {
      inGuide = true;
      const cleaned = s.replace(/^📖\s*\*\*.*?\*\*\s*/, '').replace(/^📖\s*/, '').replace(/\*\*/g, '').trim();
      if (cleaned) guideText = cleaned;
      continue;
    }
    // Chinese reading guide in blockquote
    if (s.startsWith('> **阅读指引') || s.startsWith('> **阅读提示')) {
      inGuide = true;
      const cleaned = s.replace(/^>\s*\*\*阅读指引[：:]?\*\*\s*/, '').replace(/^>\s*\*\*阅读提示[：:]?\*\*\s*/, '');
      if (cleaned) guideText = cleaned;
      continue;
    }
    if (inGuide) {
      if (s === '---' || s.startsWith('##')) { inGuide = false; continue; }
      guideText += (guideText ? ' ' : '') + s;
      continue;
    }

    if (s.startsWith('*Source:') || s.startsWith('*From:') || s.startsWith('*Adapted')) {
      sourceLine = s.replace(/^\*|\*$/g, '').trim();
      continue;
    }
    // Chinese source in blockquote: > **来源：** ...
    if (s.startsWith('> **来源')) {
      sourceLine = s.replace(/^>\s*\*\*来源[：:]?\*\*\s*/, '').replace(/\*\*/g, '');
      continue;
    }
    if (s === '📖' || s === '**📖**') continue;
    if (s.startsWith('**Argument Structure') || s.startsWith('**Argument structure')) continue;

    let processed = s.replace(/^>\s?/, '');
    processed = processed.replace(
      /\*\*\s*\[(Thesis|Premise|Evidence|Counter-?argument|Conclusion)\s*[·•][^\]]+\]\s*\*\*/g,
      (_, label) => { const cls = LABEL_MAP[label] || 'arg-thesis'; return `\x00LABEL\x00${cls}\x00${label}\x00`; }
    );
    processed = processed.replace(
      /\[(Thesis|Premise|Evidence|Counter-?argument|Conclusion)\s*[·•][^\]]+\]/g,
      (_, label) => { const cls = LABEL_MAP[label] || 'arg-thesis'; return `\x00LABEL\x00${cls}\x00${label}\x00`; }
    );
    // Bold format: **Thesis**, **Premise**, etc. (no brackets)
    processed = processed.replace(
      /\*\*(Thesis|Premise|Evidence|Counter-?argument|Conclusion)\*\*/g,
      (_, label) => { const cls = LABEL_MAP[label] || 'arg-thesis'; return `\x00LABEL\x00${cls}\x00${label}\x00`; }
    );
    passageBody += ' ' + processed;
  }

  let rendered = mdInline(esc(passageBody.trim()));
  rendered = rendered.replace(
    /\x00LABEL\x00(arg-[a-z-]+)\x00([A-Za-z-]+)\x00/g,
    (_, cls, label) => `<span class="arg-label ${cls}">${label}</span>`
  );

  let html = '';
  if (sourceLine) html += `<p class="source-line">${esc(sourceLine)}</p>`;
  html += `<div class="passage-block"><p>${rendered}</p></div>`;
  if (guideText) html += `<div class="guide-block">${mdInline(esc(guideText))}</div>`;
  return html;
}

// ── Section 3: Expressions (可迁移表达) ──
function renderExpressions(allText) {
  const html = [];
  const lines = allText.split('\n');

  const blocks = [];
  let curBlock = null;
  for (const line of lines) {
    const s = line.trim();
    if (/^###\s*(?:表达|Expression)\s*\d+/i.test(s)) {
      if (curBlock) blocks.push(curBlock);
      curBlock = { header: s, lines: [] };
    } else if (curBlock) {
      curBlock.lines.push(s);
    }
  }
  if (curBlock) blocks.push(curBlock);

  for (const block of blocks) {
    // Match both 表达 N and Expression N (case-insensitive)
    const numMatch = block.header.match(/(?:表达|Expression)\s*(\d+)/i);
    const num = numMatch ? numMatch[1] : '';

    // Detect format by checking for English metadata markers
    const allBlockText = block.lines.join('\n');
    // Only trigger English format when distinct markers (功能/语域) are present.
    // 例句 and 搭配 overlap with the current Chinese format and must NOT be used here.
    const isEnglishFormat = /-\s*\*\*功能[：:]\*\*/.test(allBlockText) ||
      /-\s*\*\*语域[：:]\*\*/.test(allBlockText) ||
      /-\s*\*\*(?:Function|Register):\*\*/.test(allBlockText);

    let tags = '';
    let phrase = '';
    let cn = '';
    let colloc = '';
    let example = '';

    if (isEnglishFormat) {
      // ── English format: > **"phrase"**, - **功能：**, - **语域：**, - **搭配链：**, - **例句：** ──
      for (const line of block.lines) {
        const s = line.trim();
        if (!s) continue;

        // Blockquote phrase: > **"phrase text"**
        if (s.startsWith('>') && s.includes('**')) {
          const phraseMatch = s.match(/>\s*\*\*["""](.+?)["""]\*\*/);
          if (phraseMatch) {
            phrase = phraseMatch[1];
          } else {
            const phraseMatch2 = s.match(/>\s*\*\*(.+?)\*\*/);
            if (phraseMatch2) phrase = phraseMatch2[1];
          }
          continue;
        }

        // Function/Register/Collocation/Example metadata
        if (/-\s*\*\*(?:功能|Function)[：:]\*\*/.test(s)) {
          tags += (tags ? ' · ' : '') + s.replace(/^-\s*\*\*(?:功能|Function)[：:]\*\*\s*/, '').trim();
          continue;
        }
        if (/-\s*\*\*(?:语域|Register)[：:]\*\*/.test(s)) {
          tags += (tags ? ' · ' : '') + s.replace(/^-\s*\*\*(?:语域|Register)[：:]\*\*\s*/, '').trim();
          continue;
        }
        if (/-\s*\*\*(?:搭配链|搭配|Collocation)[：:]\*\*/.test(s)) {
          colloc = s.replace(/^-\s*\*\*(?:搭配链|搭配|Collocation)[：:]\*\*\s*/, '').replace(/\*\*/g, '').trim();
          continue;
        }
        if (/-\s*\*\*(?:例句|Example)[：:]\*\*/.test(s)) {
          example = s.replace(/^-\s*\*\*(?:例句|Example)[：:]\*\*\s*/, '').trim();
          // Remove wrapping *italic* markers
          example = example.replace(/^\*|\*$/g, '');
          continue;
        }
        // Multi-line example continuation
        if (example && !s.startsWith('- **') && !s.startsWith('>')) {
          example += ' ' + s.replace(/^-\s*/, '').replace(/^\*|\*$/g, '');
        }
      }
    } else {
      // ── Legacy Chinese format (non-English markers) ──
      let isNewFormat = false;
      // Check if this is the current Format D (labeled inline fields)
      for (const line of block.lines) {
        if (/^\*\*英文表达[：:]/.test(line.trim())) { isNewFormat = true; break; }
      }

      let mode = 'init';
      for (const line of block.lines) {
        const s = line.trim();
        if (!s) continue;

        // ── Format D (current): labeled inline fields ──
        if (isNewFormat) {
          if (/^\*\*英文表达[：:]/.test(s)) {
            phrase = s.replace(/\*\*英文表达[：:]\*\*\s*/, '').replace(/`/g, '');
            continue;
          }
          if (/^\*\*功能标签[：:]/.test(s)) {
            tags += (tags ? ' · ' : '') + s.replace(/\*\*功能标签[：:]\*\*\s*/, '').trim();
            continue;
          }
          if (/^\*\*语域标签[：:]/.test(s)) {
            tags += (tags ? ' | ' : '') + s.replace(/\*\*语域标签[：:]\*\*\s*/, '').trim();
            continue;
          }
          if (/^\*\*中文释义[：:]/.test(s)) {
            cn = s.replace(/\*\*中文释义[：:]\*\*\s*/, '').trim();
            continue;
          }
          if (/^\*\*常见搭配[：:]/.test(s)) {
            colloc = s.replace(/\*\*常见搭配[：:]\*\*\s*/, '').trim();
            // If empty heading, next bullet lines are collocations
            if (!colloc) {
              // Content on following bullet lines
            }
            continue;
          }
          if (/^\*\*外刊例句[：:]/.test(s)) {
            example = s.replace(/\*\*外刊例句[：:]\*\*\s*/, '').replace(/^\*|\*$/g, '');
            continue;
          }
          // Bullet line with `code` → collocation (in new format)
          if (s.startsWith('- ') && s.includes('`')) {
            const codeText = s.replace(/^-\s*/, '').replace(/`/g, '');
            colloc += (colloc ? '\n' : '') + codeText;
            continue;
          }
          continue;
        }

        // ── Legacy formats ──

        if (s.includes('`') && s.includes('·') && mode === 'init') {
          const backtickMatch = s.match(/`([^`]+)`/);
          tags = backtickMatch ? backtickMatch[1] : s.replace(/`/g, '').replace(/\*\*/g, '');
          continue;
        }
        if (s.includes('**`') && s.includes('`**')) {
          const phraseMatch = s.match(/\*\*`(.+?)`\*\*/);
          phrase = phraseMatch ? phraseMatch[1] : s.replace(/\*\*/g, '').replace(/`/g, '');
          mode = 'phrase';
          continue;
        }
        if (s.startsWith('- ') && mode === 'phrase' && !s.includes('常见搭配') && !s.includes('**例句')) {
          cn = s.replace(/^-\s*/, '');
          mode = 'cn';
          continue;
        }
        if (s.includes('常见搭配') || s.includes('搭配')) {
          colloc = s.replace(/\*\*/g, '').replace(/^-\s*/, '').replace(/常见搭配[：:]\s*/, '').replace(/搭配[：:]\s*/, '');
          mode = 'colloc';
          continue;
        }
        if (s.includes('**例句') || (s.startsWith('-') && s.includes('*') && mode === 'colloc')) {
          example = s.replace(/^-\s*/, '').replace(/\*\*/g, '').replace(/例句[：:]\s*/, '');
          mode = 'example';
          continue;
        }
        if (mode === 'example') {
          example += (example ? ' ' : '') + s.replace(/^-\s*/, '');
        }
      }
    }

    html.push(
      `<div class="expr-item">` +
      (num ? `<div class="expr-num">Expression ${num}</div>` : '') +
      (phrase ? `<div class="expr-phrase">${esc(phrase)}</div>` : '') +
      (tags ? `<div class="expr-tags">${esc(tags)}</div>` : '') +
      (cn ? `<div class="expr-cn">${mdInline(esc(cn))}</div>` : '') +
      (colloc ? `<div class="expr-colloc"><strong>常见搭配：</strong>${mdInline(esc(colloc))}</div>` : '') +
      (example ? `<div class="expr-example">${mdInline(esc(example))}</div>` : '') +
      `</div>`
    );
  }

  return html.join('\n');
}

// ── Section 4: Sentence Deconstruction (句型拆解) ──
function renderSentenceDecon(allText) {
  const html = [];
  const lines = allText.split('\n');

  let mode = 'init';
  let targetSentence = '';
  let structureText = [];
  let grammarCards = [];
  let curGrammar = null;
  let templateText = '';
  let imitationText = '';
  let scenarioText = '';

  for (const line of lines) {
    const s = line.trim();
    if (!s) continue;
    if (s.startsWith('*') && !s.startsWith('**') && mode === 'init') continue;
    if (s === '---') continue;

    // Target sentence — Chinese and English headers
    if ((s.startsWith('**目标句**') || s.startsWith('**目标句：**') ||
         s.startsWith('**Target Sentence:**') || s.startsWith('**Target Sentence**')) && !s.includes('结构') && !s.includes('模板')) {
      mode = 'target';
      continue;
    }
    // Structure analysis — Chinese and English headers
    if ((s.includes('结构拆解') || s.includes('结构分析') || s.includes('**Structure:**') || s.includes('**Structure**')) && s.startsWith('**')) {
      mode = 'structure';
      continue;
    }
    // Grammar points — Chinese and English headers
    if ((s.includes('语法要点') || s.includes('语法点') || s.includes('Grammar Points')) && s.startsWith('**')) {
      mode = 'grammar';
      continue;
    }
    // Template — Chinese headers (仿写模板, 模仿模板, 结构模板, 句型模板, 模板句型)
    if ((s.includes('仿写模板') || s.includes('模仿模板') || s.includes('结构模板') || s.includes('句型模板') || s.includes('模板句型')) && s.startsWith('**')) {
      mode = 'template';
      continue;
    }
    // Imitation — Chinese and English
    if ((s.includes('你的仿写') || s.includes('仿写练习') || s.includes('Imitation:')) && s.startsWith('**')) {
      mode = 'imitation';
      continue;
    }
    // Scenario — Chinese and English
    if ((s.includes('适用场景') || s.includes('仿写场景') || s.includes('Applicable Scenarios')) && s.startsWith('**')) {
      mode = 'scenario';
      continue;
    }

    if (mode === 'target') {
      if (s.startsWith('>')) targetSentence += (targetSentence ? ' ' : '') + cleanText(s);
    } else if (mode === 'structure') {
      if (!s.startsWith('**') && !s.startsWith('---')) structureText.push(cleanText(s));
    } else if (mode === 'grammar') {
      // Match numbered grammar: 1. **title：** body or - **title：** body
      const numMatch = s.match(/^(?:\d+[.\)]\s*|-)\s*\*\*(.+?)\*\*[：:]\s*(.*)/);
      const numMatch2 = s.match(/^(?:\d+[.\)]\s*|-)\s*\*\*(.+?)[：:]\*\*\s*(.*)/);
      if (numMatch || numMatch2) {
        if (curGrammar) grammarCards.push(curGrammar);
        const m = numMatch || numMatch2;
        curGrammar = { title: m[1].trim(), body: m[2] ? [m[2].trim()] : [], code: '' };
        continue;
      }
      if (s.startsWith('```')) continue;
      if (curGrammar && s.trim() && !s.startsWith('-') && !s.startsWith('---')) {
        curGrammar.body.push(cleanText(s));
      }
    } else if (mode === 'template') {
      if (!s.startsWith('**') && !s.startsWith('---') && !s.startsWith('```')) templateText += (templateText ? '\n' : '') + cleanText(s);
    } else if (mode === 'imitation') {
      if (!s.startsWith('**') && !s.startsWith('---')) imitationText += (imitationText ? '\n' : '') + cleanText(s);
    } else if (mode === 'scenario') {
      if (!s.startsWith('**') && !s.startsWith('---')) scenarioText += (scenarioText ? ' ' : '') + cleanText(s);
    }
  }

  if (curGrammar) grammarCards.push(curGrammar);

  // Target sentence
  if (targetSentence) {
    html.push(
      `<div class="target-sentence-block">` +
      `<div class="ts-label">Target Sentence</div>` +
      `<div class="ts-text">${mdInline(esc(targetSentence))}</div>` +
      `</div>`
    );
  }

  // Why This Sentence Works (structure analysis)
  if (structureText.length > 0) {
    html.push(
      `<div class="why-block">` +
      `<div class="why-label">Why This Sentence Works</div>` +
      `<div class="why-text">${mdInline(esc(structureText.join(' ')))}</div>` +
      `</div>`
    );
  }

  // Grammar Points
  if (grammarCards.length > 0) {
    html.push(`<div class="grammar-section"><div class="grammar-section-label">Grammar Points</div>`);
    for (const gc of grammarCards) {
      html.push(
        `<div class="grammar-mini-card">` +
        `<div class="gm-title">${esc(gc.title)}</div>` +
        `<div class="gm-body">${mdInline(esc(gc.body.join(' ')))}</div>` +
        (gc.code ? `<pre class="gm-code">${esc(gc.code)}</pre>` : '') +
        `</div>`
      );
    }
    html.push(`</div>`);
  }

  // Reusable Template
  if (templateText) {
    html.push(
      `<div class="template-block">` +
      `<div class="tpl-label">Reusable Template</div>` +
      `<pre class="tpl-code">${esc(templateText)}</pre>` +
      `</div>`
    );
  }

  // Imitation Example
  if (imitationText) {
    html.push(
      `<div class="imitation-block">` +
      `<div class="imit-label">Imitation Example</div>` +
      `<div class="imit-text">${mdInline(esc(imitationText))}</div>` +
      `</div>`
    );
  }

  // Applicable Scenarios
  if (scenarioText) {
    html.push(`<span class="scenario-tag">${esc(scenarioText)}</span>`);
  }

  return `<div class="sentence-decon">${html.join('\n')}</div>`;
}

// ── Section 5: Argument Chain (中文观点 → 英文 Argument Chain) ──
function renderArgumentChain(allText) {
  const html = [];
  const lines = allText.split('\n');

  let cnViewpoint = '';
  let coreConcept = '';
  let causalChain = '';
  let weighingParas = [];
  let samplePara = '';
  let sampleNote = '';

  let mode = 'init';
  let curWeighing = [];

  for (const line of lines) {
    const s = line.trim();
    if (!s) {
      if (mode === 'weighing' && curWeighing.length) {
        weighingParas.push(curWeighing.join(' '));
        curWeighing = [];
      }
      continue;
    }

    if (s.startsWith('*') && !s.startsWith('**') && mode === 'init') continue;

    if ((s.includes('中文观点') && s.startsWith('**')) || s.startsWith('🇨🇳')) {
      mode = 'cn';
      continue;
    }
    if (((s.includes('Core Concept') || s.includes('EN Core')) && s.startsWith('**')) || s.startsWith('🏗️')) {
      mode = 'core';
      continue;
    }
    if ((s.includes('Causal Chain') && s.startsWith('**')) || s.startsWith('⛓️')) {
      mode = 'causal';
      continue;
    }
    if ((s.includes('Weighing') || s.includes('权衡')) && (s.startsWith('**') || s.startsWith('⚖️'))) {
      mode = 'weighing';
      continue;
    }
    if ((s.includes('Sample Argument') || s.includes('Sample Paragraph') || s.includes('示范段落')) && (s.startsWith('**') || s.startsWith('✍️'))) {
      mode = 'sample';
      continue;
    }
    if (s.startsWith('📌') || s.startsWith('**📌')) {
      sampleNote = cleanText(s);
      continue;
    }
    if (s === '---') continue;

    if (mode === 'cn') {
      cnViewpoint += (cnViewpoint ? ' ' : '') + cleanText(s);
    } else if (mode === 'core') {
      coreConcept += (coreConcept ? ' ' : '') + cleanText(s);
    } else if (mode === 'causal') {
      if (s.startsWith('```')) continue;
      causalChain += (causalChain ? '\n' : '') + cleanText(s);
    } else if (mode === 'weighing') {
      curWeighing.push(cleanText(s));
    } else if (mode === 'sample') {
      samplePara += (samplePara ? ' ' : '') + cleanText(s);
    }
  }

  if (mode === 'weighing' && curWeighing.length) {
    weighingParas.push(curWeighing.join(' '));
  }

  html.push('<div class="chain-flow">');

  if (cnViewpoint) {
    html.push(
      `<div class="chain-step">` +
      `<div class="step-label">Chinese Viewpoint</div>` +
      `<div class="step-cn">${mdInline(esc(cnViewpoint))}</div>` +
      `</div>`
    );
  }
  if (coreConcept) {
    html.push(
      `<div class="chain-step">` +
      `<div class="step-label">English Core Concept</div>` +
      `<div class="step-en">${mdInline(esc(coreConcept))}</div>` +
      `</div>`
    );
  }
  if (causalChain) {
    html.push(
      `<div class="chain-step">` +
      `<div class="step-label">Causal Chain</div>` +
      `<pre class="step-code">${esc(causalChain)}</pre>` +
      `</div>`
    );
  }
  if (weighingParas.length > 0) {
    const paras = weighingParas.map(p => `<p>${mdInline(esc(p))}</p>`).join('');
    html.push(
      `<div class="weighing-block">` +
      `<div class="weigh-label">Weighing</div>` +
      `<div class="weigh-text">${paras}</div>` +
      `</div>`
    );
  }
  if (samplePara) {
    html.push(
      `<div class="sample-para-block">` +
      `<div class="sp-label">Sample Argument Paragraph</div>` +
      `<div class="sp-text en-body">${mdInline(esc(samplePara))}</div>` +
      (sampleNote ? `<div class="sp-note">${mdInline(esc(sampleNote))}</div>` : '') +
      `</div>`
    );
  }

  html.push('</div>');
  return html.join('\n');
}

// ── Section 6: Output Tasks (输出任务) ──
function renderOutputTasks(allText) {
  const html = [];
  const lines = allText.split('\n');

  // Task-level structures
  const tasks = [];
  const premiumHints = [];

  let currentTask = null;
  let mode = 'init';
  let inPremium = false;

  for (const line of lines) {
    const s = line.trim();
    if (!s) continue;
    if (s.startsWith('*') && !s.startsWith('**') && !inPremium) continue;
    if (s === '---') continue;

    // Task headers: ### Task 1/A: ... / ### Task 2/B: ...
    if (/^###\s*Task\s*[1A]/.test(s)) {
      if (currentTask) tasks.push(currentTask);
      const taskType = (s.includes('写作') || s.includes('Writing')) ? 'Writing Task' : 'Speaking Task';
      const metaMatch = s.match(/[（(]([^)）]+)[)）]/);
      currentTask = { type: taskType, prompt: '', guide: [], check: [], meta: metaMatch ? metaMatch[1] : '' };
      mode = 'init';
      inPremium = false;
      continue;
    }
    if (/^###\s*Task\s*[2B]/.test(s)) {
      if (currentTask) tasks.push(currentTask);
      const taskType = (s.includes('口语') || s.includes('Speaking')) ? 'Speaking Task' : 'Writing Task';
      const metaMatch = s.match(/[（(]([^)）]+)[)）]/);
      currentTask = { type: taskType, prompt: '', guide: [], check: [], meta: metaMatch ? metaMatch[1] : '' };
      mode = 'init';
      inPremium = false;
      continue;
    }

    // Modern: ### IELTS Writing Task 2 / ### Speaking / ### Writing / ### 写作 / ### 口语
    if (/^###\s*(?:IELTS\s|Writing|Speaking|口语|写作)/.test(s)) {
      if (currentTask) tasks.push(currentTask);
      const taskType = (s.includes('写作') || s.includes('Writing')) ? 'Writing Task' : 'Speaking Task';
      const metaMatch = s.match(/[（(]([^)）]+)[)）]/);
      currentTask = { type: taskType, prompt: '', guide: [], check: [], meta: metaMatch ? metaMatch[1] : '' };
      mode = 'init';
      inPremium = false;
      continue;
    }

    // Shared guide: ### 结构指引 / ### 结构引导 / ### Structure Guide
    if (/^###\s*(?:结构指引|结构引导|结构指南|Structure Guide|Speaking Guide|思维拓展)/.test(s)) {
      mode = 'guide';
      continue;
    }
    // Shared check: ### 自检清单 / ### Self-Check
    if (/^###\s*(?:自检清单|Self[- ]?[Cc]heck)/.test(s)) {
      mode = 'check';
      continue;
    }

    // Legacy: **写作任务...**
    if ((s.startsWith('**写作任务') || s.startsWith('**IELTS Task') || s.startsWith('**写作任务')) && !s.includes('Speaking')) {
      if (currentTask) tasks.push(currentTask);
      currentTask = { type: 'Writing Task', prompt: '', guide: [], check: [], meta: '' };
      let task = s;
      task = task.replace(/\*\*写作任务[（(][^)）]*[)）]\*\*\s*/, '');
      task = task.replace(/\*\*写作任务[：:]\*\*\s*/, '');
      task = task.replace(/\*\*写作任务[^*]*\*\*\s*/, '');
      task = task.replace(/\*\*IELTS[^*]*\*\*\s*/, '');
      const metaMatch = task.match(/[（(]建议\s*.+[）)]/);
      if (metaMatch) { currentTask.meta = metaMatch[0]; task = task.replace(metaMatch[0], '').trim(); }
      if (task.trim()) currentTask.prompt = cleanText(task);
      mode = 'prompt';
      continue;
    }

    // Legacy: **口语任务...**
    if ((s.startsWith('**口语任务') || s.startsWith('**IELTS Part 3') || s.startsWith('**口语')) && !s.includes('写作')) {
      if (currentTask) tasks.push(currentTask);
      currentTask = { type: 'Speaking Task', prompt: '', guide: [], check: [], meta: '' };
      let task = s;
      task = task.replace(/\*\*口语任务[（(][^)）]*[)）]\*\*\s*/, '');
      task = task.replace(/\*\*口语任务[：:]\*\*\s*/, '');
      task = task.replace(/\*\*口语(?:任务|训练|表达)[^*]*\*\*\s*/, '');
      task = task.replace(/\*\*IELTS[^*]*\*\*\s*/, '');
      const metaMatch = task.match(/[（(]建议\s*.+[）)]/);
      if (metaMatch) { currentTask.meta = metaMatch[0]; task = task.replace(metaMatch[0], '').trim(); }
      if (task.trim()) currentTask.prompt = cleanText(task);
      mode = 'prompt';
      continue;
    }

    // Premium section
    if (s.startsWith('### 参考答案提示') || s.startsWith('### 参考') || s.includes('Premium')) {
      if (currentTask) { tasks.push(currentTask); currentTask = null; }
      inPremium = true;
      mode = 'premium';
      continue;
    }

    // Sub-headers — Topic / Question (used in Task A/B format)
    if (s.startsWith('**Topic:**') || s.startsWith('**Topic**') || s.startsWith('**题目：**') || s.startsWith('**题目**')) {
      mode = 'prompt';
      const inline = s.replace(/\*\*(?:Topic|题目)[：:]?\*\*\s*/, '');
      if (inline.trim() && currentTask) currentTask.prompt = cleanText(inline);
      continue;
    }
    if (s.startsWith('**Question:**') || s.startsWith('**Question**')) {
      mode = 'prompt';
      const inline = s.replace(/\*\*(?:Question)[：:]?\*\*\s*/, '');
      if (inline.trim() && currentTask) currentTask.prompt = cleanText(inline);
      continue;
    }
    // Structure Guide — Chinese and English (do NOT flush task, just switch mode)
    if (s.startsWith('**结构引导') || s.startsWith('**结构指引') || s.startsWith('**Structure Guide') || s.startsWith('**结构指南') || s.startsWith('**思维拓展')) {
      mode = 'guide';
      continue;
    }
    // Speaking Guide (do NOT flush task)
    if (s.startsWith('**Speaking Guide')) {
      mode = 'guide';
      continue;
    }
    // Self-Check — Chinese and English (do NOT flush task)
    if (s.startsWith('**Self-Check') || s.startsWith('**Self-check') || s.startsWith('**自我检查') || s.startsWith('**Self-check 清单') || s.startsWith('**自测清单')) {
      mode = 'check';
      continue;
    }

    // Content collection
    if (mode === 'prompt' && currentTask) {
      if (s.startsWith('>')) currentTask.prompt += (currentTask.prompt ? ' ' : '') + cleanText(s);
    } else if (mode === 'guide' && currentTask) {
      if (s.startsWith('- ')) currentTask.guide.push(cleanText(s));
      else if (/^\d+[.\)]\s/.test(s)) currentTask.guide.push(cleanText(s));
    } else if (mode === 'check' && currentTask) {
      if (s.startsWith('- [ ]') || s.startsWith('- [x]') || s.startsWith('- [X]')) {
        currentTask.check.push(cleanText(s));
      } else if (s.startsWith('- □') || s.startsWith('- ☐')) {
        const cleaned = s.replace(/^-\s*[□☐☑☒✓✔✗✘]\s*/, '');
        if (cleaned) currentTask.check.push(cleaned);
      } else if (s.startsWith('- ')) {
        currentTask.check.push(cleanText(s));
      }
    } else if (mode === 'premium' || inPremium) {
      if (s && !s.startsWith('---')) premiumHints.push(cleanText(s));
    }
  }

  if (currentTask) tasks.push(currentTask);

  // Post-processing: if multiple tasks exist and later tasks captured
  // guide/check items (shared sections like ### 结构指引 / ### 自检清单),
  // propagate to earlier tasks that are missing them.
  if (tasks.length > 1) {
    const last = tasks[tasks.length - 1];
    for (const t of tasks.slice(0, -1)) {
      if (!hasContent(t.guide) && hasContent(last.guide)) t.guide = [...last.guide];
      if (!hasContent(t.check) && hasContent(last.check)) t.check = [...last.check];
    }
  }

  // ── Render task blocks with per-task guide and check ──
  for (const task of tasks) {
    if (!hasContent(task.prompt)) continue;

    html.push('<div class="task-block">');

    // Task header
    html.push(
      `<div class="task-header">` +
      `<span class="task-type">${esc(task.type)}</span>` +
      (hasContent(task.meta) ? `<span class="task-meta">${esc(task.meta)}</span>` : '') +
      `</div>`
    );

    // Prompt
    if (hasContent(task.prompt)) {
      html.push(`<div class="task-prompt">${mdInline(esc(task.prompt))}</div>`);
    }

    // Per-task Structure Guide
    if (hasContent(task.guide)) {
      const stepItems = task.guide.map((item, i) => {
        return `<div class="step-item">` +
          `<span class="step-number">${i + 1}</span>` +
          `<div class="step-content">${mdInline(esc(item))}</div>` +
          `</div>`;
      }).join('');

      html.push(
        `<div class="guide-card">` +
        `<div class="guide-label">Structure Guide</div>` +
        `<div class="structure-guide">${stepItems}</div>` +
        `</div>`
      );
    }

    // Per-task Self-Check
    if (hasContent(task.check)) {
      const checkItems = task.check.map(item => {
        let text = item.replace(/^\[[ xX]\]\s*/, '');
        text = text.replace(/^[□☑☒✓✔✗✘]\s*/, '');
        if (!text.trim()) return '';
        return `<div class="check-item">` +
          `<span class="check-box"></span>` +
          `<span class="check-text">${mdInline(esc(text))}</span>` +
          `</div>`;
      }).filter(Boolean).join('');

      if (checkItems) {
        html.push(
          `<div class="check-card self-check-section">` +
          `<div class="check-label self-check-title">Self-Check</div>` +
          `<div class="self-check-list">${checkItems}</div>` +
          `</div>`
        );
      }
    }

    html.push('</div>');
  }

  // ── Premium hints (only if they exist) ──
  if (hasContent(premiumHints)) {
    const hintsHtml = premiumHints.map(h => mdInline(esc(h))).join('<br>');
    html.push(
      `<div class="premium-hint-card">` +
      `<div class="ph-label">Premium 参考答案提示</div>` +
      `<div class="ph-text">${hintsHtml}</div>` +
      `</div>`
    );
  }

  return html.join('\n');
}

// ── Notes Box (standard, for after each major section) ──
function renderNotesBox() {
  return `<div class="notes-box">
    <div class="notes-header">
      <span class="notes-title">Notes</span>
      <span class="notes-hint">Key ideas · Questions · Examples</span>
    </div>
    <div class="notes-body"></div>
  </div>`;
}

// ── Cornell Notes (for deep learning sections) ──
function renderCornellNotes() {
  return `<div class="cornell-notes">
    <div class="cornell-header">
      <span class="cornell-title">Notes</span>
      <span class="cornell-hint">Cornell-style reflection</span>
    </div>
    <div class="cornell-main">
      <div class="cornell-cue">
        <div class="cornell-label">Cue / Keywords</div>
      </div>
      <div class="cornell-note">
        <div class="cornell-label">Notes / Examples</div>
      </div>
    </div>
    <div class="cornell-summary">
      <div class="cornell-label">Summary / Takeaway</div>
    </div>
  </div>`;
}

// ── My Argument Draft (end of output tasks) ──
function renderDraftArea() {
  return `<div class="draft-area">
    <div class="draft-header">My Argument Draft</div>
    <div class="draft-main">
      <div class="draft-outline">
        <div class="do-label">Outline</div>
        <div class="do-item"><strong>Thesis</strong></div>
        <div class="do-item"><strong>Key expressions</strong></div>
        <div class="do-item"><strong>Counter-argument</strong></div>
        <div class="do-item"><strong>Final sentence</strong></div>
      </div>
      <div class="draft-writing">
        <div class="dw-label">My paragraph / speaking outline</div>
      </div>
    </div>
    <div class="draft-takeaway">
      <div class="dt-label">Final takeaway</div>
    </div>
  </div>`;
}

// ═══════════════════════════════════════════════════════════════════════
// MAIN RENDER FUNCTION
// ═══════════════════════════════════════════════════════════════════════

function renderPrintHtml(mdText, issueDate) {
  // Strip YAML frontmatter
  if (mdText.startsWith('---')) {
    const end = mdText.indexOf('---', 3);
    if (end !== -1) mdText = mdText.slice(end + 3).trim();
  }

  const lines = mdText.split('\n');

  // Extract metadata
  let title = 'ArgueLab Training Briefing';
  let topicLine = '';
  let trainingFocus = '';
  let issueSlug = '';
  let dateStr = '';

  if (issueDate) {
    const d = new Date(issueDate + 'T00:00:00');
    dateStr = d.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
    issueSlug = '#' + issueDate.replace(/-/g, '');
  }

  for (const line of lines.slice(0, 15)) {
    const s = line.trim();
    if (s.startsWith('# ') && !s.startsWith('## ')) {
      title = s.replace(/^#\s+/, '');
      continue;
    }
    const m1 = s.match(/^>\s*\*\*今日议题：\*\*\s*(.+)$/);
    if (m1) { topicLine = m1[1].trim(); }
    const m2 = s.match(/^>\s*\*\*训练重点：\*\*\s*(.+)$/);
    if (m2) trainingFocus = m2[1].trim();
  }

  // Parse sections
  const sections = [];
  let curTitle = null;
  let curLines = [];
  let inCode = false;
  let codeBuf = [];

  for (let i = 0; i < lines.length; i++) {
    const s = lines[i].trim();

    if (s.startsWith('```')) {
      if (inCode) { curLines.push({ type: 'code', text: codeBuf.join('\n') }); codeBuf = []; inCode = false; }
      else { inCode = true; }
      continue;
    }
    if (inCode) { codeBuf.push(lines[i]); continue; }

    if (s.startsWith('# ') && !s.startsWith('## ')) continue;
    if (s === '---') continue;

    if (/^##\s+\d+\./.test(s)) {
      if (curTitle !== null) sections.push({ title: curTitle, items: curLines });
      curTitle = s.replace(/^##\s+\d+\.\s*/, '');
      curLines = [];
    } else if (s.startsWith('## ') && !s.startsWith('### ')) {
      if (curTitle !== null) sections.push({ title: curTitle, items: curLines });
      curTitle = s.slice(3).trim();
      curLines = [];
    } else {
      curLines.push({ type: 'text', text: s });
    }
  }
  if (curTitle !== null) sections.push({ title: curTitle, items: curLines });

  const MODULES = ['context', 'passage', 'expression', 'sentence', 'chain', 'output'];

  // ── Build HTML ──
  let body = '';

  // HEADER
  body += `<header class="pdf-header">` +
    `<div class="slug">${esc(issueSlug)}</div>` +
    `<div class="brand">Argue<span>Lab</span></div>` +
    `<div class="subtitle">Read like a scholar. Argue like a native.</div>` +
    `<h1 style="display:none;">${esc(title)}</h1>` +
    `<div class="meta-line">` +
      `<span>${esc(dateStr)}</span>` +
      (trainingFocus ? `<span class="meta-sep">·</span><span>${esc(trainingFocus)}</span>` : '') +
      (topicLine ? `<span class="meta-sep">·</span><span>${esc(topicLine)}</span>` : '') +
    `</div>` +
    `</header>`;

  const sectionMods = [
    { cls: 'context', label: 'Section' },
    { cls: 'passage', label: 'Section' },
    { cls: 'expression', label: 'Section' },
    { cls: 'sentence', label: 'Section' },
    { cls: 'chain', label: 'Section' },
    { cls: 'output', label: 'Section' },
  ];

  for (let idx = 0; idx < sections.length; idx++) {
    const sec = sections[idx];
    const mod = MODULES[idx] || 'context';
    const num = idx + 1;
    const secCls = sectionMods[idx] ? sectionMods[idx].cls : 'context';

    // Skip sections beyond the 6 defined modules (e.g. source metadata appendix)
    if (idx >= MODULES.length) break;

    body += `<section class="issue-section">`;
    body += `<div class="section-title">` +
      `<span class="sec-num sec-num-${secCls}">${String(num).padStart(2, '0')}</span>` +
      `${esc(sec.title)}` +
      `</div>`;

    const allText = sec.items.map(item => item.type === 'code' ? item.text : item.text).join('\n');

    if (mod === 'context') {
      body += renderContext(allText);
      // ❌ NO Notes Box after context (background is pre-reading material)
    } else if (mod === 'passage') {
      body += renderPassage(allText);
      body += renderNotesBox();
    } else if (mod === 'expression') {
      body += renderExpressions(allText);
      body += renderNotesBox();
    } else if (mod === 'sentence') {
      body += renderSentenceDecon(allText);
      body += renderCornellNotes();
    } else if (mod === 'chain') {
      body += renderArgumentChain(allText);
      body += renderCornellNotes();
    } else if (mod === 'output') {
      // Order: Task blocks → Structure Guide → Self-check → Draft Area
      // (NO Cornell Notes in output section)
      body += renderOutputTasks(allText);
      body += renderDraftArea();
    }

    body += '</section>';
  }

  // FOOTER
  body += `<footer class="page-footer">` +
    `<p class="brand">Argue<span>Lab</span></p>` +
    `<p class="slogan">Read like a scholar. Argue like a native.</p>` +
    `<p class="gen-date">Generated ${esc(dateStr)}</p>` +
    `</footer>`;

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>${esc(topicLine || 'ArgueLab')}</title>
<style>${PRINT_CSS}</style>
</head>
<body>
<div class="page-wrapper"><div class="page">${body}</div></div>
</body>
</html>`;
}

// ═══════════════════════════════════════════════════════════════════════
// MAIN — Puppeteer PDF generation
// ═══════════════════════════════════════════════════════════════════════

async function main() {
  const args = process.argv.slice(2);
  if (args.length < 2) {
    console.error('Usage: node render-pdf.js <briefing.md> <output.pdf>');
    process.exit(1);
  }

  const mdPath = path.resolve(args[0]);
  const outPath = path.resolve(args[1]);

  if (!fs.existsSync(mdPath)) {
    console.error('File not found:', mdPath);
    process.exit(1);
  }

  const dateMatch = mdPath.match(/(\d{4}-\d{2}-\d{2})/);
  const issueDate = dateMatch ? dateMatch[1] : '';

  console.error('[pdf] Rendering', mdPath, '...');
  const mdText = fs.readFileSync(mdPath, 'utf-8');
  const html = renderPrintHtml(mdText, issueDate);

  // Debug: write HTML to temp file (for inspection if needed)
  const tmpHtml = outPath.replace(/\.pdf$/, '.tmp.html');
  fs.writeFileSync(tmpHtml, html, 'utf-8');

  console.error('[pdf] Launching Puppeteer...');
  let browser;
  try {
    browser = await puppeteer.launch({
      headless: true,
      executablePath: findChromium(),
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    });
    const page = await browser.newPage();
    // Use setContent instead of file:// to avoid UTF-8 encoding issues with local files
    await page.setContent(html, { waitUntil: 'load', timeout: 60000 });

    await page.pdf({
      path: outPath,
      format: 'A4',
      printBackground: true,
      margin: { top: '22mm', bottom: '20mm', left: '22mm', right: '22mm' },
      displayHeaderFooter: false,
      preferCSSPageSize: false,
    });

    console.error('[pdf] ✓', outPath);
  } catch (err) {
    console.error('[pdf] FAILED:', err.message);
    process.exit(1);
  } finally {
    if (browser) await browser.close();
    try { fs.unlinkSync(tmpHtml); } catch (_) {}
  }
}

if (require.main === module) {
  main();
}

module.exports = {
  renderPrintHtml,
  renderExpressions,
  renderSentenceDecon,
  renderArgumentChain,
  renderOutputTasks,
  renderContext,
  renderPassage,
  esc,
  mdInline,
  cleanText,
  stripMarkdown,
};
