# Multi-stage Dockerfile for the Machina MCP server (streamable HTTP)

# A base image, e.g. one pinned by digest: python:3.11-slim-bookworm@sha256:<digest>
ARG PYTHON_BASE=python:3.11-slim-bookworm

FROM ${PYTHON_BASE} AS build

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential libffi-dev libssl-dev && \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/machina-venv
ENV PATH="/opt/machina-venv/bin:$PATH"

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
COPY deploy/docker/constraints.txt ./

# The MCP server makes no LLM calls: the default extras are the REST CMMS
# client and the MCP SDK. Add what your connectors need, e.g.
#   docker compose build --build-arg MACHINA_EXTRAS=cmms-rest,mcp,docs-rag,excel
ARG MACHINA_EXTRAS=cmms-rest,mcp
# MACHINA_CONSTRAINTS=constraints.txt pins every install to the committed versions.
ARG MACHINA_CONSTRAINTS=
# TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu installs CPU-only torch and
# torchvision; a docs-rag-pro build made this way was checked to contain no NVIDIA
# wheels. Both come from that index alone and without dependencies, which then
# resolve from PyPI with the extras.
ARG TORCH_INDEX_URL=

RUN if [ -n "$TORCH_INDEX_URL" ]; then \
        constraint=""; \
        if [ -n "$MACHINA_CONSTRAINTS" ]; then constraint="-c $MACHINA_CONSTRAINTS"; fi; \
        pip install --no-cache-dir --no-deps --index-url "$TORCH_INDEX_URL" $constraint \
            torch torchvision; \
    fi
RUN if [ -n "$MACHINA_CONSTRAINTS" ]; then \
        pip install --no-cache-dir -c "$MACHINA_CONSTRAINTS" ".[${MACHINA_EXTRAS}]"; \
    else \
        pip install --no-cache-dir ".[${MACHINA_EXTRAS}]"; \
    fi

# --- Runtime stage ---
FROM ${PYTHON_BASE} AS runtime

RUN groupadd --system machina && \
    useradd --system --gid machina --create-home machina

COPY --from=build /opt/machina-venv /opt/machina-venv
ENV PATH="/opt/machina-venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /home/machina
USER machina

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Listen on all interfaces inside the container; docker-compose.yml publishes
# the port on the host's loopback only.
ENTRYPOINT ["machina", "mcp", "serve"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000", "--config", "/home/machina/config.yaml"]
