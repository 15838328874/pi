# syntax=docker/dockerfile:1
# pi-py multi-user server image
# Supports PostgreSQL (asyncpg) and MySQL (aiomysql); PI_DATABASE_URL is required.

FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
# runtime deps only (db drivers + redis + serving + migrations + milvus/mcp clients)
RUN pip install --no-cache-dir asyncpg aiomysql redis uvicorn alembic pymilvus mcp

COPY --from=builder /install /usr/local
# Bake migrations into the image so `pi-py migrate` works without the repo.
COPY alembic.ini migrations /opt/pi-py/
RUN useradd --create-home --uid 10001 pi && mkdir -p /home/pi/.pi-py && chown -R pi:pi /home/pi
USER pi
WORKDIR /home/pi

ENV PI_TRACER=jsonl \
    PI_ALEMBIC_DIR=/opt/pi-py \
    PYTHONUNBUFFERED=1

EXPOSE 8300
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8300/healthz',timeout=3).status==200 else 1)"

CMD ["python", "-m", "pi.cli", "serve", "--host", "0.0.0.0", "--port", "8300"]
