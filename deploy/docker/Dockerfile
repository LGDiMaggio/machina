# Multi-stage Dockerfile for Machina MCP server
# Target: <600 MB uncompressed

FROM python:3.11-slim-bookworm AS build

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential libffi-dev libssl-dev && \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/machina-venv
ENV PATH="/opt/machina-venv/bin:$PATH"

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

RUN pip install --no-cache-dir ".[cmms-rest,litellm,docs-rag,mcp]"

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

ENTRYPOINT ["python", "-m", "machina.mcp"]
CMD ["--transport", "streamable-http", "--config", "/home/machina/config.yaml"]
