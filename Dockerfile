# ArgueLab — Production Docker Image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ── System deps: chromium (for PDF) + curl (for Node.js install) ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates chromium \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── Node.js 22 (prebuilt binary, needed for Puppeteer) ──
RUN curl -fsSL https://nodejs.org/dist/v22.12.0/node-v22.12.0-linux-x64.tar.xz \
    -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && node --version && npm --version

# ── Python deps ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Puppeteer-core (no bundled Chromium — uses system chromium) ──
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
