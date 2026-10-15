FROM python:3.12-slim

# lark-cli / wecom-cli 是 npm 包；tapd-cli 为独立二进制，需要时挂载到 /usr/local/bin/tapd-cli
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ripgrep git ca-certificates \
    && npm install -g @larksuite/cli @wecom/cli \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY app ./app
COPY config ./config
COPY migrations ./migrations
COPY main.py utils.py ./
RUN pip install --no-cache-dir .

COPY scripts ./scripts
COPY skills ./skills
COPY deploy ./deploy

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "app.bootstrap"]
