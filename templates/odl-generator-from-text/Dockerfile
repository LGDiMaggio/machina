FROM python:3.11-slim-bookworm AS builder

WORKDIR /build
RUN python -m venv /opt/machina-venv
ENV PATH="/opt/machina-venv/bin:$PATH"

RUN pip install --no-cache-dir "machina-ai[cmms-rest,litellm,docs-rag,mcp]==0.3.0"

# ---

FROM python:3.11-slim-bookworm

RUN useradd --system --create-home machina
COPY --from=builder /opt/machina-venv /opt/machina-venv
ENV PATH="/opt/machina-venv/bin:$PATH"

WORKDIR /home/machina/app
COPY --chown=machina:machina . .

USER machina
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

ENTRYPOINT ["python", "agent.py"]
