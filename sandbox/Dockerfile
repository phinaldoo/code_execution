# ============================================================
# Sandbox Container — Executes untrusted Python code securely
# ============================================================
# This image is used as an ephemeral container by the gateway.
# Each code execution gets its own container instance that is
# destroyed after completion.
#
# Build: docker build -t code-sandbox -f Dockerfile .
# ============================================================

FROM python:3.12-slim AS base

# --- System Setup ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        # Required for some scientific packages
        libgomp1 \
        # Fonts for matplotlib
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# --- Python Packages ---
# Install common data science & utility packages
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

# --- Pre-compile matplotlib font cache ---
RUN python -c "import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt; plt.figure(); plt.close()" 2>/dev/null || true

# --- Non-root User ---
RUN groupadd -r sandbox && \
    useradd -r -g sandbox -d /home/sandbox -s /sbin/nologin sandbox && \
    mkdir -p /home/sandbox /tmp/code /tmp/output && \
    chown -R sandbox:sandbox /home/sandbox /tmp/code /tmp/output

# --- Executor Script ---
COPY executor.py /usr/local/bin/executor.py
RUN chmod +x /usr/local/bin/executor.py

# --- Environment ---
ENV MPLBACKEND=Agg \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    HOME=/home/sandbox

# --- Runtime ---
USER sandbox
WORKDIR /tmp/code

ENTRYPOINT ["python", "/usr/local/bin/executor.py"]
