FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/savely

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

RUN useradd --system --no-create-home savely
USER savely

EXPOSE 8000

# The API serves no WebSockets. With the protocol left on, uvicorn logs the client
# address and query string of every WebSocket handshake, so it is switched off.
CMD ["uvicorn", "app.main:create_app", "--factory", "--ws", "none", "--host", "0.0.0.0", "--port", "8000"]
