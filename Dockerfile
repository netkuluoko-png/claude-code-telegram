FROM python:3.11-slim

# Install Node.js 20.x (required by claude-agent-sdk) + system deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl git ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

# Install Python dependencies via pip (no Poetry needed)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/
COPY setup.cfg ./

# Copy entrypoint
COPY entrypoint.sh ./
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

# Unpack TGLogistAgent project into /project
COPY project.tar.gz /tmp/project.tar.gz
RUN mkdir -p /project && tar xzf /tmp/project.tar.gz -C /project/ && rm /tmp/project.tar.gz

# Create non-root user
RUN useradd -m -s /bin/bash claude && \
    mkdir -p /app/data && \
    chown -R claude:claude /app /project

# Entrypoint runs as root to fix volume permissions, then drops to claude user
CMD ["./entrypoint.sh"]
