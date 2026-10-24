FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app

RUN useradd --uid 10001 --no-create-home appuser
USER appuser

CMD exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8080} --no-access-log --proxy-headers --forwarded-allow-ips='*'
