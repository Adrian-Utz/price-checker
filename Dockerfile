FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PRICE_CHECKER_MODE=public \
    PRICE_CHECKER_HOST=0.0.0.0 \
    PRICE_CHECKER_PORT=5000 \
    PRICE_CHECKER_DATA_DIR=/data

# A fixed unprivileged UID so bind-mounted data directories can be chown'd to it (chown 10001:10001 ./data).
RUN useradd --system --uid 10001 --user-group --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir /app /data \
    && chown app:app /app /data

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=app:app . .
RUN sed -i 's/\r$//' docker/entrypoint.sh && chmod +x docker/entrypoint.sh

USER app

# Database, secret key, self-signed certificates, backups and the provider choice all live here.
VOLUME /data
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=3)"]

ENTRYPOINT ["docker/entrypoint.sh"]
# One worker process with several threads (PRICE_CHECKER_THREADS): SQLite, scan locks and the scheduler share one process.
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
