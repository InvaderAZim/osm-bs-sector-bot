FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN addgroup --system duga && adduser --system --ingroup duga duga \
    && chown -R duga:duga /app

USER duga

CMD ["sh", "-c", "exec uvicorn entrypoint:api --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --timeout-keep-alive 15"]
