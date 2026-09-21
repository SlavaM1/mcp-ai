FROM python:3.12.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --upgrade pip==25.2 && pip install -r requirements.txt

COPY backend/alembic.ini ./alembic.ini
COPY backend/alembic ./alembic
COPY backend/app ./app

FROM base AS test
COPY backend/requirements-dev.txt ./requirements-dev.txt
RUN pip install -r requirements-dev.txt
COPY backend/tests ./tests
CMD ["sh", "-c", "alembic upgrade head && python -m pytest -q"]

FROM base AS runtime
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
