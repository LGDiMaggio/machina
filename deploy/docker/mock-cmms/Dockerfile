ARG PYTHON_BASE=python:3.11-slim-bookworm

FROM ${PYTHON_BASE}

COPY constraints.txt ./
# MACHINA_CONSTRAINTS=constraints.txt pins the install to the committed versions.
ARG MACHINA_CONSTRAINTS=
RUN if [ -n "$MACHINA_CONSTRAINTS" ]; then \
        pip install --no-cache-dir -c "$MACHINA_CONSTRAINTS" fastapi uvicorn; \
    else \
        pip install --no-cache-dir fastapi uvicorn; \
    fi

COPY app.py /app/app.py
WORKDIR /app

EXPOSE 9000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
