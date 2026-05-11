# ============================================================
# API Gateway — Manages sandbox containers for code execution
# ============================================================
# Build: docker build -t code-gateway -f Dockerfile .
# ============================================================

ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm@sha256:dba9a3ac4914b5ae6f790188ff39267e46e0087fa0f4713592b5d32350edb5ef
FROM ${PYTHON_BASE_IMAGE} AS base

# --- System Setup ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get clean

# --- Python Dependencies ---
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

# --- Non-root User ---
RUN groupadd -r gateway && \
    useradd -r -g gateway -d /home/gateway -s /sbin/nologin gateway && \
    mkdir -p /home/gateway && \
    chown -R gateway:gateway /home/gateway

# --- Application ---
COPY *.py /app/
WORKDIR /app

# --- Environment ---
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/gateway \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# --- Runtime ---
EXPOSE 8000

USER gateway:gateway

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "info"]
