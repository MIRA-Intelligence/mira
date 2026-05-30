FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
ARG BUILD_BRIDGE=1

# Install Node.js 20 for the WhatsApp bridge
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git bubblewrap openssh-client && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p mira_engine bridge && touch mira_engine/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf mira_engine bridge

# Copy the full source and install
COPY mira_engine/ mira_engine/
COPY bridge/ bridge/
RUN uv pip install --system --no-cache .

# Build the WhatsApp bridge
WORKDIR /app/bridge
RUN git config --global --add url."https://github.com/".insteadOf ssh://git@github.com/ && \
    git config --global --add url."https://github.com/".insteadOf git@github.com: && \
    git config --global --add url."https://github.com/".insteadOf git+ssh://git@github.com/ && \
    git config --global --add url."https://github.com/".insteadOf ssh://github.com/ && \
    git config --global --add url."https://github.com/".insteadOf git://github.com/ && \
    if [ "$BUILD_BRIDGE" = "1" ]; then \
      npm install && npm run build; \
    else \
      echo "Skipping bridge build (BUILD_BRIDGE=$BUILD_BRIDGE)"; \
    fi
WORKDIR /app

# Create non-root user and config directory
RUN useradd -m -u 1000 -s /bin/bash mira && \
    mkdir -p /home/mira/.mira && \
    chown -R mira:mira /home/mira /app

COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

USER mira
ENV HOME=/home/mira

# Gateway default port
EXPOSE 18790

ENTRYPOINT ["entrypoint.sh"]
CMD ["status"]
