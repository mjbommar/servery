ARG PYTHON_IMAGE=python:3.15.0b3-slim
FROM ${PYTHON_IMAGE}

ARG GUNICORN_VERSION=26.0.0
ARG UVICORN_VERSION=0.51.0
ARG STARLETTE_VERSION=1.3.1
ARG FASTAPI_VERSION=
ARG UVLOOP_VERSION=
ARG HTTPTOOLS_VERSION=

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/servery
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY benchmarks ./benchmarks

RUN python -m pip install --no-cache-dir . \
    "gunicorn==${GUNICORN_VERSION}" \
    "uvicorn==${UVICORN_VERSION}" \
    "starlette==${STARLETTE_VERSION}"

# FastAPI/Pydantic is optional because current pydantic-core does not publish a
# CPython 3.15 wheel.  Use --build-arg FASTAPI_VERSION=... with a compatible
# Python image (currently 3.14) rather than silently adding a compiler toolchain
# to the standard 3.15 comparison.
RUN if [ -n "${FASTAPI_VERSION}" ]; then \
        python -m pip install --no-cache-dir "fastapi==${FASTAPI_VERSION}"; \
    fi

# Native Uvicorn acceleration is also opt-in because uvloop and httptools do
# not currently publish CPython 3.15 wheels. Use a labeled Python 3.14 cohort.
RUN if [ -n "${UVLOOP_VERSION}" ] && [ -n "${HTTPTOOLS_VERSION}" ]; then \
        python -m pip install --no-cache-dir \
            "uvloop==${UVLOOP_VERSION}" "httptools==${HTTPTOOLS_VERSION}"; \
    fi
