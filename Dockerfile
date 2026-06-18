# ArgueLab — Production Docker Image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ── System deps ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    libnss3 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── Node.js 22 (prebuilt binary) ──
RUN curl -fsSL https://nodejs.org/dist/v22.12.0/node-v22.12.0-linux-x64.tar.xz \
    -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && node --version && npm --version

# ── Python deps ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Puppeteer ──
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
