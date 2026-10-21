FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates libcurl4 libssl3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ /app/src/
COPY bin/ /app/bin/
RUN chmod +x /app/bin/*.py

RUN useradd -m -u 1000 app && chown -R app:app /app
# /data : le volume nomme herite de ce proprietaire au premier montage
RUN mkdir -p /data && chown app:app /data
USER app

ENTRYPOINT ["python3", "-m", "avito.cli"]
CMD ["--help"]
