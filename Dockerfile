ARG PYTHON_VERSION=3.14

FROM ghcr.io/astral-sh/uv:python$PYTHON_VERSION-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libc6-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PYTHON_DOWNLOADS=0

WORKDIR /build
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev
ADD . /build
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:$PYTHON_VERSION-slim-bookworm

# Links the published image to this repository on GHCR. Without it the
# package stays unattached, and the release workflow's GITHUB_TOKEN is
# refused with "permission_denied: write_package" when pushing.
LABEL org.opencontainers.image.source="https://github.com/Free-Guy-IR/panel"

COPY --from=builder /build /code
WORKDIR /code

ENV PATH="/code/.venv/bin:$PATH"

# Keep the runtime trust store explicit. Outbound notification clients use it
# without replacing Python's process-wide SSLContext.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && sed -i -E 's@^(mozilla/(emSign|eMudhra)[^/]*\.crt)$@!\1@' /etc/ca-certificates.conf \
    && update-ca-certificates --fresh \
    && rm -rf /var/lib/apt/lists/*

RUN base_python="$(python -c 'import sys; print(sys.base_prefix)')/bin/python3" \
    && ensurepip_dir="$("$base_python" -c 'import ensurepip, pathlib; print(pathlib.Path(ensurepip.__file__).parent)')" \
    && test -d "$ensurepip_dir" \
    && rm -f "$ensurepip_dir"/_bundled/pip-*.whl \
    && PG_IMAGE_BUILD=1 python -B /code/scripts/distrust_certifi_roots.py --image

RUN python -B /code/scripts/image_trust_store_check.py

COPY cli_wrapper.sh /usr/bin/pasarguard-cli
RUN chmod +x /usr/bin/pasarguard-cli

COPY tui_wrapper.sh /usr/bin/pasarguard-tui
RUN chmod +x /usr/bin/pasarguard-tui

# Copy healthcheck script
COPY healthcheck.sh /code/healthcheck.sh
RUN chmod +x /code/healthcheck.sh

RUN chmod +x /code/start.sh

ENTRYPOINT ["/code/start.sh"]
