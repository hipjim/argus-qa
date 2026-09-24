# argus-qa server: POST a test plan or scenario, it runs in a headless browser.
#
#   docker build -t argus-qa .
#   docker run -p 8080:8080 -e ANTHROPIC_API_KEY=... -e ARGUS_API_KEY=... \
#     -v argus-data:/data argus-qa
FROM node:22-bookworm-slim

# Keep in sync with PLAYWRIGHT_MCP_PACKAGE in argus_qa/agents/orchestrator.py
ARG PLAYWRIGHT_MCP_VERSION=0.0.82

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Browser system libraries (needs root)
RUN npx -y "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}" --version \
    && cd "$(dirname "$(find /root/.npm/_npx -path '*/node_modules/playwright-core/package.json' | head -1)")" \
    && node cli.js install-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.npm

RUN useradd --create-home --shell /bin/bash argus \
    && mkdir -p /data && chown argus:argus /data
USER argus
WORKDIR /home/argus

# Warm the npx cache and download the browser build this MCP version expects
RUN npx -y "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}" --version \
    && cd "$(dirname "$(find ~/.npm/_npx -path '*/node_modules/playwright-core/package.json' | head -1)")" \
    && node cli.js install chromium

COPY --chown=argus:argus pyproject.toml README.md ./app/
COPY --chown=argus:argus argus_qa ./app/argus_qa
RUN python3 -m venv ~/venv && ~/venv/bin/pip install --no-cache-dir "./app[server]"

ENV PATH="/home/argus/venv/bin:${PATH}" \
    ARGUS_PLAYWRIGHT_ARGS="--browser chromium --no-sandbox" \
    PYTHONUNBUFFERED=1

EXPOSE 8080
VOLUME /data
CMD ["argus-qa", "serve", "--host", "0.0.0.0", "--port", "8080", "--data-dir", "/data"]
