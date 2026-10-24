FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements-production.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-production.lock \
    && python -m pip check \
    && groupadd --gid 10001 matchos \
    && useradd --uid 10001 --gid matchos --no-create-home matchos
COPY --chown=matchos:matchos matchos/ ./matchos/
USER 10001:10001
EXPOSE 8000
# Session state and speech registration are process-local: exactly one worker.
CMD ["python", "-m", "uvicorn", "matchos.server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "30"]
