FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps for git, SSH, and Playwright Chromium
# Install fonts manually to avoid playwright --with-deps failures on slim images
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git openssh-client curl ca-certificates \
       libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
       libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
       libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
       libpango-1.0-0 libcairo2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium \
    && rm -rf /root/.cache/pip /tmp/*

COPY . .
RUN chmod +x /app/start.sh

EXPOSE 8080
CMD ["/app/start.sh"]
