# ---- Stage 1: Frontend build ----
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: Python runtime ----
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev libcairo2 libcairo2-dev pkg-config \
    libpango-1.0-0 libpangocairo-1.0-0 curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY schemas/ ./schemas/
COPY tenants/ ./tenants/
COPY docs/ ./docs/
RUN pip install --no-cache-dir .

# Frontend dist van stage 1
COPY --from=frontend-build /app/frontend/dist /app/static

# Non-root user voor security
RUN adduser --disabled-password --gecos '' appuser
RUN mkdir -p /app/uploads /app/data && chown -R appuser:appuser /app

# Multi-tenant: alle tenant directories beschikbaar voor brand resolution.
# Default tenant is de neutrale open-source placeholder onder tenants/default.
# Productie-deployments kunnen een andere tenant bind-mounten en de env
# vars OPENAEC_DEFAULT_BRAND + OPENAEC_TENANT_DIR overschrijven.
ENV OPENAEC_TENANTS_ROOT=/app/tenants
ENV OPENAEC_TENANT_DIR=/app/tenants/default
ENV OPENAEC_DEFAULT_BRAND=default

# Build-marker: bak de git-commit in het image zodat /api/health objectief
# aangeeft welke code draait. Zet bij de build:
#   docker build --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) ...
ARG GIT_COMMIT=unknown
ENV OPENAEC_BUILD=${GIT_COMMIT}

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "openaec_reports.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
