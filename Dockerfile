# ArgueLab — Production Docker Image
# Base: slim Python 3.12 + Node.js 22 (for PDF generation via Puppeteer)
FROM python:3.12-slim

# Prevent Python from writing .pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ── System deps: Node.js 22 + Chromium libraries (for Puppeteer) ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg ca-certificates \
    # Chromium/Puppeteer runtime deps
    libnss3 libnspr4 libatk-bridge2.0-0 libatk1.0-0 libcups2 \
    libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
    libcairo2 libx11-xcb1 libxcb1 libxext6 libxrender1 \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── Python deps ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Node.js deps (Puppeteer for PDF generation) ──
COPY scripts/package.json scripts/package.json
RUN cd /app/scripts && npm install --omit=dev

# ── Application code ──
COPY server.py .
COPY static/ static/
COPY briefings/ briefings/
COPY scripts/render-pdf.js scripts/render-pdf.js
# data/ is created at runtime by the app (DATA_DIR.mkdir)
RUN mkdir -p /app/data /app/pdf

# Create a non-root user for security
RUN useradd --create-home --shell /bin/bash arguelab && \
    chown -R arguelab:arguelab /app
USER arguelab

# Health check every 30s
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')" || exit 1

EXPOSE 8080

CMD ["python", "server.py", "--serve"]
