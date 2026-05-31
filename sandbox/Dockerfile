# ============================================================
# Sandbox Container — Executes untrusted Python code securely
# ============================================================
# This image is used for bounded sandbox sessions managed by the gateway.
# A session can run multiple serialized executions and is destroyed after
# idle timeout, hard lifetime, execution budget exhaustion, or client delete.
#
# Build: docker build -t code-sandbox -f Dockerfile .
# ============================================================

ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm@sha256:dba9a3ac4914b5ae6f790188ff39267e46e0087fa0f4713592b5d32350edb5ef
FROM ${PYTHON_BASE_IMAGE} AS base

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# --- System Setup ---
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
        # Required for some scientific packages
        libgomp1 \
        # Fonts for matplotlib
        fonts-dejavu-core \
        # Node runtime for editable slide rendering
        nodejs \
        npm \
        # Reaps orphaned child processes for long-lived sandbox sessions
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# --- Python Packages ---
# Install common data science & utility packages
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

# --- Playwright Browser Runtime ---
# Install Chromium + required system libraries at build time so sandbox
# sessions and slide renders share one browser cache without runtime downloads.
RUN python -m playwright install --with-deps chromium

# --- Slide Renderer Node Dependencies ---
# The Node renderer uses the same Playwright version and browser cache as the
# Python runtime. Do not run `playwright install` here; the browser is already
# installed above under PLAYWRIGHT_BROWSERS_PATH.
COPY v2/package.json v2/package-lock.json /opt/code-execution/v2/
RUN npm --prefix /opt/code-execution/v2 ci --omit=dev --ignore-scripts --no-audit --no-fund

# --- Pre-compile matplotlib font cache ---
RUN python -c "import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt; plt.figure(); plt.close()" 2>/dev/null || true

# --- Non-root User ---
RUN groupadd -r -g 10001 sandbox && \
    useradd -r -u 10001 -g sandbox -d /home/sandbox -s /sbin/nologin sandbox && \
    mkdir -p /home/sandbox /tmp/code /tmp/output /ms-playwright && \
    chown -R sandbox:sandbox /home/sandbox /tmp/code /tmp/output /ms-playwright

# --- Executor Script ---
COPY executor.py /usr/local/bin/executor.py
COPY render_presentation.py /usr/local/bin/render_presentation.py
COPY slide_renderer /opt/code-execution/slide_renderer
COPY v1 /opt/code-execution/v1
COPY v2/index.js /opt/code-execution/v2/index.js
RUN chmod +x /usr/local/bin/executor.py /usr/local/bin/render_presentation.py

# --- Environment ---
ENV MPLBACKEND=Agg \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONPATH=/opt/code-execution \
    XDG_CACHE_HOME=/tmp/.cache \
    HOME=/home/sandbox

# --- Runtime ---
USER sandbox
WORKDIR /home/sandbox
ENTRYPOINT ["/usr/bin/tini", "--"]

# Keep the session alive between executions; tini handles child reaping.
CMD ["sleep", "infinity"]
