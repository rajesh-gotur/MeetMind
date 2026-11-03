FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY run.py config.yaml ./

RUN useradd --create-home appuser
USER appuser

# Render sets $PORT; run.py reads it (it overrides server.port in config.yaml).
CMD ["python", "run.py"]
