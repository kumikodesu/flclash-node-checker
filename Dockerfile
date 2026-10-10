# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS core
ADD --checksum=sha256:135da95baaf41f077521b98c0170554da432348b2e3aee73d6f4f050119e7917 \
    https://github.com/chen08209/FlClash/releases/download/v0.8.93/FlClash-0.8.93-linux-amd64.deb /tmp/flclash.deb
RUN echo '135da95baaf41f077521b98c0170554da432348b2e3aee73d6f4f050119e7917  /tmp/flclash.deb' | sha256sum --check \
    && dpkg-deb --extract /tmp/flclash.deb /tmp/flclash \
    && mkdir /core \
    && find /tmp/flclash -type f -name FlClashCore -exec cp '{}' /core/FlClashCore \; \
    && test -s /core/FlClashCore && chmod 755 /core/FlClashCore

FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update && apt-get -o Acquire::Retries=3 install -y --no-install-recommends chromium ca-certificates \
    && useradd --create-home --uid 10001 checker \
    && mkdir -p /data/results && chown -R checker:checker /data
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=core /core/FlClashCore /opt/flclash/FlClashCore
WORKDIR /app
COPY vendor ./vendor
COPY bootstrap.py checker.py core.py iplark.py iplark_browser.mjs report.py report.html webui.py webui.html ./
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    FLCLASH_CONFIG=/flclash/config.yaml CHECKER_OUTPUT=/data/results IPLARK_CACHE=/data/iplark-cache.json
USER checker
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"
CMD ["python", "-E", "-s", "webui.py"]
