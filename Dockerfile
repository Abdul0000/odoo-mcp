FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHROME_BIN=chromium \
    ODOO_AGENT_CDP_URL=http://127.0.0.1:9222

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git openssh-client rsync curl ca-certificates \
       chromium fonts-liberation libnss3 libatk-bridge2.0-0 libxkbcommon0 \
       libgtk-3-0 libasound2 libgbm1 libxss1 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

# Node.js is required for real JS/OWL syntax parsing (node --check), not just
# regex heuristics. Minimal install: no npm global packages, no build toolchain.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /root/.cache/pip /tmp/*

COPY . .
RUN chmod +x /app/start.sh

EXPOSE 8080
CMD ["/app/start.sh"]
