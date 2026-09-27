FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt \
    && useradd --create-home --uid 10001 appuser

COPY --chown=appuser:appuser currency_mcp ./currency_mcp

USER appuser
EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/health', timeout=2).read()"]

CMD ["python", "-m", "currency_mcp"]

FROM runtime AS test

COPY --chown=appuser:appuser tests ./tests

USER appuser
CMD ["python", "-m", "unittest", "discover", "-s", "tests"]
