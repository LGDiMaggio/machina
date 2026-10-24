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

# litellm<1.106: the litellm extra of 0.4.0 has no upper bound, and litellm
# 1.106 fails every tool call unless mcp 2.x is installed (#160). Drop it once
# the pin moves to a release whose litellm extra carries the bound.
RUN pip install --no-cache-dir "machina-ai[excel,litellm]==0.4.0" "litellm<1.106"

# The mounted ./data keeps the host's ownership on Linux, so the container user
# takes the host user's UID/GID (docker-compose.yml passes them; 1000 is the
# first regular user on most distributions). --no-log-init keeps useradd from
# writing lastlog/faillog records sized by the UID, which a large UID turns
# into a huge image layer.
ARG MACHINA_UID=1000
ARG MACHINA_GID=1000
RUN groupadd --non-unique --gid "$MACHINA_GID" machina \
    && useradd --no-log-init --non-unique --uid "$MACHINA_UID" --gid "$MACHINA_GID" \
        --create-home machina
WORKDIR /home/machina/app
COPY --chown=machina:machina . .
USER machina

ENTRYPOINT ["python", "agent.py"]
