FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git openssh-client rsync curl ca-certificates \
       libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
       libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
       libxdamage1 libxfixes3 libxrandr2 libgbm1 \
       libpango-1.0-0 libcairo2 libatspi2.0-0 \
    && apt-get install -y --no-install-recommends libasound2t64 || \
       apt-get install -y --no-install-recommends libasound2 || true \
    && rm -rf /var/lib/apt/lists/*

# Node.js is required for real JS/OWL syntax parsing (node --check), not just
# regex heuristics. Minimal install: no npm global packages, no build toolchain.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium \
    && rm -rf /root/.cache/pip /tmp/*

COPY . .
RUN chmod +x /app/start.sh

EXPOSE 8080
CMD ["/app/start.sh"]
