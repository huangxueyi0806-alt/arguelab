# ArgueLab — Production Docker Image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ── All system deps in one layer ──
# chromium + CJK fonts (required for Chinese characters in PDF)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm chromium \
    fonts-noto-cjk \
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
