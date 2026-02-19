# ============================================================
# API Gateway — Manages sandbox containers for code execution
# ============================================================
# Build: docker build -t code-gateway -f Dockerfile .
# ============================================================

FROM python:3.12-slim

# --- System Setup ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# --- Python Dependencies ---
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

# --- Non-root User ---
RUN groupadd -r gateway && \
    useradd -r -g gateway -d /home/gateway -s /sbin/nologin gateway && \
    mkdir -p /home/gateway && \
    chown -R gateway:gateway /home/gateway

# --- Application ---
COPY app.py /app/app.py
WORKDIR /app

# --- Environment ---
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# --- Runtime ---
EXPOSE 8000

# The gateway needs to run as root to access Docker socket
# Security is handled at the container management level
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "info"]
