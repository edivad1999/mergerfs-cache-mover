FROM python:3.14-slim

ENV DOCKER_CONTAINER=1 \
    PYTHONUNBUFFERED=1 \
    WEB_UI_HOST=0.0.0.0 \
    WEB_UI_PORT=9090 \
    STATUS_PATH=/var/log/cache-mover-status.json

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    cron \
    procps \
    && rm -rf /var/lib/apt/lists/* && \
    mkdir -p /var/log && \
    touch /var/log/cache-mover.log

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD test -f /var/run/crond.pid && ps -p $(cat /var/run/crond.pid) -o comm= | grep -q '^cron$' && python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"WEB_UI_PORT\", \"9090\")}/healthz', timeout=2).read()" || exit 1

CMD ["docker-entrypoint.sh"]
