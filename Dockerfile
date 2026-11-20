FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/savely

# The indexing worker runs ffprobe (is there an audio track?) and ffmpeg (the
# stored copies, the audio for speech-to-text). The API image is the same image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY app ./app
RUN pip install .

# The worker keeps media in /srv/savely/media (MEDIA_STORAGE_DIR in Compose). A
# new named volume takes over this folder's owner, so the worker can write to it.
RUN useradd --system --no-create-home savely \
    && mkdir -p /srv/savely/media \
    && chown savely /srv/savely/media
USER savely

EXPOSE 8000

# The API serves no WebSockets. With the protocol left on, uvicorn logs the client
# address and query string of every WebSocket handshake, so it is switched off.
CMD ["uvicorn", "app.main:create_app", "--factory", "--ws", "none", "--host", "0.0.0.0", "--port", "8000"]
