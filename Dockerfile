# ArgueLab — Production Docker Image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ── All system deps in one layer ──
# chromium + CJK fonts (Chinese character rendering in PDF)
# Strategy: WQY Zen Hei (reliable, small) as base Chinese font
# Noto CJK (large) as premium serif CJK font if build succeeds
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm chromium \
    fonts-wqy-zenhei \
    fonts-wqy-microhei \
    && ln -sf /usr/bin/nodejs /usr/local/bin/node \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && fc-cache -fv 2>/dev/null || true

# ── Python deps ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Puppeteer-core (uses system chromium) ──
COPY scripts/package.json scripts/package.json
RUN cd /app/scripts && npm install

# ── App code ──
COPY server.py .
COPY supabase_client.py .
COPY static/ static/
COPY briefings/ briefings/
COPY scripts/render-pdf.js scripts/render-pdf.js
RUN mkdir -p /app/data /app/pdf

RUN useradd --create-home --shell /bin/bash arguelab && chown -R arguelab:arguelab /app
USER arguelab

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')" || exit 1

EXPOSE 8080
CMD ["python", "server.py", "--serve"]
