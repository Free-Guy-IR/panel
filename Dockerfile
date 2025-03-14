ARG PYTHON_VERSION=3.12

FROM python:$PYTHON_VERSION-slim

# Set up the working directory

RUN mkdir /code
WORKDIR /code
COPY . /code

RUN sed -i 's/\r$//' /code/marzban-cli.py

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

COPY cli-wrapper.sh /usr/bin/marzban-cli
RUN chmod +x /usr/bin/marzban-cli
RUN /usr/bin/marzban-cli completion install --shell bash

# Set the entrypoint
ENTRYPOINT ["bash", "-c", "uv run alembic upgrade head"]
CMD ["bash", "-c", "uv run main.py"]
