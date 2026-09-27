# ELSTER MCP Server (Python) – Streamable HTTP mit Bearer-Token
# Die Playwright-Version im Basis-Image MUSS zur installierten Python-Playwright-Version passen.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ELSTER_MCP_TRANSPORT=http \
    ELSTER_MCP_HOST=0.0.0.0 \
    ELSTER_MCP_PORT=8765 \
    ELSTER_DOWNLOAD_DIR=/data/downloads \
    ELSTER_SCREENSHOT_DIR=/data/screenshots \
    ELSTER_AUDIT_LOG=/data/audit/elster-audit.jsonl \
    # Der Container ist die Isolationsgrenze; die Chromium-eigene Sandbox braucht
    # User-Namespaces, die Docker standardmäßig nicht freigibt.
    ELSTER_CHROMIUM_SANDBOX=0

WORKDIR /app
COPY pyproject.toml README.md ./
COPY elster_mcp ./elster_mcp
RUN pip install --no-cache-dir . "playwright==1.55.0" \
 && mkdir -p /data && chown -R pwuser:pwuser /data

USER pwuser
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8765), 3)" || exit 1

ENTRYPOINT ["elster-mcp", "serve"]
