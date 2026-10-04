# Multi-stage Dockerfile for the Machina MCP server (streamable HTTP)

FROM python:3.11-slim-bookworm AS build

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential libffi-dev libssl-dev && \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/machina-venv
ENV PATH="/opt/machina-venv/bin:$PATH"

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

# The MCP server makes no LLM calls: the default extras are the REST CMMS
# client and the MCP SDK. Add what your connectors need, e.g.
#   docker compose build --build-arg MACHINA_EXTRAS=cmms-rest,mcp,docs-rag,excel
ARG MACHINA_EXTRAS=cmms-rest,mcp
RUN pip install --no-cache-dir ".[${MACHINA_EXTRAS}]"

# --- Runtime stage ---
FROM python:3.11-slim-bookworm AS runtime

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
