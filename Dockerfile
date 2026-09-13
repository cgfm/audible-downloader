ARG PYTHON_BASE="python:3.12.13-slim-trixie@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"

FROM ${PYTHON_BASE} AS dependencies

COPY requirements.lock /tmp/requirements.lock
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install \
        --disable-pip-version-check \
        --no-cache-dir \
        --require-hashes \
        --requirement /tmp/requirements.lock \
    && rm -rf /opt/venv/lib/python3.12/site-packages/pip* \
        /opt/venv/bin/pip*

FROM ${PYTHON_BASE} AS test

COPY requirements-dev.lock /tmp/requirements-dev.lock
RUN python -m pip install \
        --disable-pip-version-check \
        --no-cache-dir \
        --require-hashes \
        --requirement /tmp/requirements-dev.lock
COPY ruff.toml /src/ruff.toml
COPY app/ /src/app/
COPY tests/ /src/tests/
WORKDIR /src
CMD ["python", "-m", "pytest", "-q"]

FROM ${PYTHON_BASE}

LABEL org.opencontainers.image.title="audible-downloader"
LABEL org.opencontainers.image.description="Downloads and converts Audible audiobooks to M4B"
LABEL org.opencontainers.image.source="https://github.com/cgfm/audible-downloader"

ENV AUDIBLE_CONFIG_DIR=/config \
    AUDIOBOOK_DIRECTORY=/audiobooks \
    AUDIBLE_WORK_DIRECTORY=/work \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_PORT=8080

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip* /usr/local/bin/pip* \
    && groupadd --gid 1000 audible \
    && useradd --uid 1000 --gid 1000 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin audible \
    && mkdir -p /app /audiobooks /config /work \
    && chown -R 1000:1000 /audiobooks /config /work \
    && chmod 0700 /audiobooks /config /work

COPY --from=dependencies /opt/venv /opt/venv
COPY --chown=0:0 app/ /app/
RUN chmod -R a-w /app /opt/venv

WORKDIR /app
USER 1000:1000

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('WEB_PORT', '8080') + '/readyz', timeout=3).read()"]

CMD ["python", "/app/wsgi.py"]
