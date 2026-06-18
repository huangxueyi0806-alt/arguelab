# ArgueLab — Production Docker Image
# Base: slim Python 3.12 (small, fast, well-supported)
FROM python:3.12-slim

# Prevent Python from writing .pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install only what's needed
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY server.py .
COPY static/ static/

# Create a non-root user for security
RUN useradd --create-home --shell /bin/bash arguelab && \
    chown -R arguelab:arguelab /app
USER arguelab

# Health check every 30s
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')" || exit 1

EXPOSE 8080

CMD ["python", "server.py", "--serve"]
