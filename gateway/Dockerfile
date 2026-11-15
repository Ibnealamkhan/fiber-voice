FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates libopencore-amrnb0 libopencore-amrwb0 libvo-amrwbenc0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

# Explicit copies and .dockerignore keep provisioning and recordings out of images.
COPY *.py ./
COPY static ./static
COPY tests ./tests
RUN groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid gateway --no-create-home gateway \
    && install -d -m 0700 -o gateway -g gateway /app/.runtime
USER gateway

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/registration/status', timeout=3).close()"

CMD ["python", "server.py"]
