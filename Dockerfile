FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TABLE_MIKU_DATA_DIR=/data \
    KNOWLEDGE_ASSISTANT_DB=/data/knowledge_assistant_2.db \
    KNOWLEDGE_ASSISTANT_HOST=0.0.0.0 \
    KNOWLEDGE_ASSISTANT_PORT=8080

WORKDIR /app

COPY requirements-ka2.txt ./
RUN python -m pip install --no-cache-dir -r requirements-ka2.txt \
    && groupadd --system --gid 10001 tablemiku \
    && useradd --system --uid 10001 --gid tablemiku --home-dir /nonexistent --shell /usr/sbin/nologin tablemiku \
    && mkdir -p /data \
    && chown tablemiku:tablemiku /data

COPY --chown=tablemiku:tablemiku table_miku ./table_miku

USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).read()"]

CMD ["python", "-m", "table_miku.knowledge_assistant.api"]
