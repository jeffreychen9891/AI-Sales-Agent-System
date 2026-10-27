ARG PYTHON_BASE_IMAGE=python:3.12-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc
FROM ${PYTHON_BASE_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN useradd --create-home --uid 10001 fastagent
WORKDIR /app

COPY requirements.lock ./requirements.lock
RUN python -m pip install --require-hashes -r requirements.lock

COPY pyproject.toml requirements.in ./
COPY packages ./packages
COPY services ./services
COPY profiles ./profiles
COPY infrastructure/migrations ./infrastructure/migrations
COPY scripts/migrate.py ./scripts/migrate.py
RUN python -m pip install --no-deps .

USER 10001:10001
CMD ["python", "-m", "fastagent_v2.worker.cli"]
