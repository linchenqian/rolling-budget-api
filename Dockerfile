# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv

RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /build
COPY pyproject.toml README.md ./
RUN mkdir -p src/rolling_budget_api && touch src/rolling_budget_api/__init__.py
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && pip install .
COPY src ./src
RUN rm -rf /build/build /build/src/rolling_budget_api.egg-info \
    && pip install --no-cache-dir --no-deps --force-reinstall .


FROM builder AS test

COPY alembic.ini ./
COPY migrations ./migrations
COPY tests ./tests
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[dev]"
CMD ["pytest", "-q"]


FROM python:${PYTHON_VERSION}-slim AS runtime

ENV APP_ENV=production \
    AUTO_MIGRATE=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts/container-entrypoint.sh ./container-entrypoint.sh

RUN chmod 0555 /app/container-entrypoint.sh

# TrueNAS ixVolumes are created with root-owned default permissions. Running as
# root keeps the Guided Custom App path usable without a custom user or ACL.
# The deployment must remain unprivileged and mount only its dedicated /data volume.
USER 0:0
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.getenv('PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health/live', timeout=3).read()"

CMD ["/app/container-entrypoint.sh"]
