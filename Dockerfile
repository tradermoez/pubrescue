FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-draw \
    libreoffice-writer \
    libreoffice-impress \
    fonts-liberation \
    fonts-dejavu \
    fonts-crosextra-carlito \
    fonts-crosextra-caladea \
    poppler-utils \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY worker/requirements.txt .
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir -r requirements.txt

COPY worker/ /app/

ENV PORT=8000
EXPOSE 8000
CMD ["/bin/sh", "-c", "/venv/bin/uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
