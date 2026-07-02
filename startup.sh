#!/bin/bash
# Railway startup script: restore briefings from issues.json before starting server
# This ensures _serve_issue_page() can find .md files even if they weren't in the Docker image

set -e

BRIEFINGS_DIR="/app/briefings"
DATA_DIR="/app/data"
ISSUES_FILE="$DATA_DIR/issues.json"

echo "[startup] Checking for issues.json..."

if [ -f "$ISSUES_FILE" ]; then
    echo "[startup] Found issues.json, restoring .md files to $BRIEFINGS_DIR..."
    
    python3 - <<'PYEOF'
import json, os, pathlib

issues_file = os.environ.get("ISSUES_FILE", "/app/data/issues.json")
briefings_dir = os.environ.get("BRIEFINGS_DIR", "/app/briefings")
pathlib.Path(briefings_dir).mkdir(parents=True, exist_ok=True)

with open(issues_file) as f:
    issues = json.load(f)

restored = 0
for issue in issues:
    slug = issue.get("slug", "")
    content_json = issue.get("content_json", {})
    raw_md = content_json.get("raw_markdown", "")
    if slug and raw_md:
        out_path = os.path.join(briefings_dir, f"{slug}-briefing.md")
        with open(out_path, "w") as f:
            f.write(raw_md)
        restored += 1
        print(f"[startup] Restored: {slug}-briefing.md")

print(f"[startup] Total restored: {restored} briefings")
PYEOF
    
    echo "[startup] Done restoring briefings."
else
    echo "[startup] No issues.json found at $ISSUES_FILE, skipping restore."
fi

echo "[startup] Starting server..."
exec python3 /app/server.py --serve
