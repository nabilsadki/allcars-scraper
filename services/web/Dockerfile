FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py dockerctl.py progress.py ./
COPY templates/ /app/templates/
COPY static/ /app/static/

RUN useradd -m -u 1000 app && chown -R app:app /app
USER app

EXPOSE 8080
CMD ["python3", "app.py"]
