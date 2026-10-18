# Standalone MCP server image (FR-E3). Serves the production ASGI app factory
# (src.handler:_build_app) under uvicorn — no AWS account, credentials, or
# Secrets Manager access required (AC-E3.7). Multi-stage + non-root (AC-E3.1),
# serving /mcp on port 8000 (AC-E3.2). The runtime image matches the Lambda
# runtime and Dockerfile.integration at python:3.12-slim (HIGH-11 / NFR-6.4).

# ---- builder: install deps into a self-contained venv ----
FROM python:3.12-slim AS builder
WORKDIR /app
# Dependencies first for layer caching. requirements.txt carries the pinned
# runtime deps (httpx, mcp/fastmcp, mangum); uvicorn is the Docker server only
# and is NOT a runtime dependency of the Lambda package (AC-E4.3), so it is
# installed explicitly here and never added to requirements.txt.
COPY requirements.txt pyproject.toml ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
 && /opt/venv/bin/pip install --no-cache-dir "uvicorn[standard]>=0.30"

# ---- runtime: copy the venv + source, run as a non-root user ----
FROM python:3.12-slim AS runtime
RUN useradd --create-home --uid 10001 appuser
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
# Application source only (the image carries runtime source + deps, not tests,
# infra, or docs — enforced by .dockerignore, AC-E3.5). src.handler resolves
# via PYTHONPATH=/app.
COPY src/ ./src/
ENV PATH="/opt/venv/bin:$PATH" PYTHONPATH=/app
USER appuser
EXPOSE 8000
# Single worker: StreamableHTTPSessionManager.run() may be called only once per
# FastMCP instance, and _build_app() creates a fresh instance per factory call,
# so one worker keeps exactly one instance alive per process.
# --proxy-headers / --forwarded-allow-ips lets the app honor the forwarded
# scheme/host behind a TLS proxy (HIGH-10). Exec form (JSON array) is required
# so the process receives signals directly (no shell wrapper).
CMD ["uvicorn", "src.handler:_build_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
