# odl-generator-from-text — container image for the interactive agent.
#
#   docker compose run --rm machina           # sandbox (default)
#   docker compose run --rm machina --live    # writes data/workorders.xlsx
#
# The agent talks on the terminal, so run it interactively (`run`, not `up`).

FROM python:3.11-slim-bookworm

RUN python -m venv /opt/machina-venv
ENV PATH="/opt/machina-venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir "machina-ai[excel,litellm]==0.4.0"

RUN useradd --system --create-home machina
WORKDIR /home/machina/app
COPY --chown=machina:machina . .
USER machina

ENTRYPOINT ["python", "agent.py"]
