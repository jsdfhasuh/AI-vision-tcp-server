# syntax=docker/dockerfile:1
# No fixed platform: build natively on Oracle ARM64 or an AMD64 Linux host.
ARG PYTHON_IMAGE=python:3.13-slim-trixie
FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
# Keep SQLite on the distribution's security update channel.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libsqlite3-0 \
 && rm -rf /var/lib/apt/lists/*
COPY requirements-web.txt ./
RUN python -m pip install --no-cache-dir -r requirements-web.txt
COPY competition ./competition
COPY web_server.py database_tools.py ./
COPY tools/init_web_admin.py tools/healthcheck.py ./tools/
RUN groupadd --gid 10001 vision \
 && useradd --uid 10001 --gid vision --no-create-home --shell /usr/sbin/nologin vision \
 && mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
ENV DATA_DIR=/data WEB_ADMIN_FILE=/run/secrets/admin.json \
    WEB_HOST=0.0.0.0 WEB_PORT=9080 TCP_HOST=0.0.0.0 TCP_PORT=9000
EXPOSE 9080 9000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
 CMD ["python", "tools/healthcheck.py"]
CMD ["python", "web_server.py"]
