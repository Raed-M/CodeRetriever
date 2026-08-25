# Cloud Run image. Kept deliberately plain: no build step, no compiled deps.
FROM python:3.12-slim

# PYTHONUNBUFFERED matters on Cloud Run: without it, log lines can sit in a
# buffer and never reach Cloud Logging if the container is scaled to zero.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY codebot/ ./codebot/

# Run as a non-root user.
RUN useradd --create-home --uid 1001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# --workers 1: the dedup set, the single-flight lock and the cooldown map are
#   in-process state. A second worker process would have its own copy of all
#   three, and two workers could hand out two codes at once.
# --threads 4: a /code request occupies its thread for up to 45 seconds, so the
#   worker must still be able to answer /start and the health check meanwhile.
# --timeout 120: matches the Cloud Run request timeout.
# Shell form on purpose, so that ${PORT} injected by Cloud Run is expanded.
CMD exec gunicorn --bind 0.0.0.0:${PORT:-8080} \
    --workers 1 \
    --threads 4 \
    --timeout 120 \
    --graceful-timeout 30 \
    --keep-alive 65 \
    --access-logfile - \
    --error-logfile - \
    main:app
