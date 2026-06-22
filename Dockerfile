# syntax=docker/dockerfile:1

FROM python:3.11-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m pip install --no-cache-dir --prefix=/install "flask>=2.3.0"

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /install /usr/local

RUN useradd --create-home --shell /usr/sbin/nologin botuser

WORKDIR /app

# Keep the build context lean with .dockerignore entries such as:
# __pycache__/, *.pyc, .git/, *.db, tests/, and .env.
COPY funding_bot.py /app/funding_bot.py
COPY web /app/web

RUN mkdir -p /app/data && chown -R botuser:botuser /app

USER botuser

EXPOSE 5000

CMD ["python", "-m", "funding_bot", "send-daily-summary", "--dry-run"]
