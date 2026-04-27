FROM python:3.11-slim

# Install Node.js 20.x (required by claude-agent-sdk) + system deps
# `sudo` is used by the bot's /update command to run one specific npm command as root.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl git ca-certificates sudo && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install agent CLIs
RUN npm install -g @anthropic-ai/claude-code @openai/codex

WORKDIR /app

# Install Python dependencies via pip (no Poetry needed)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code + MCP config
COPY src/ ./src/
COPY setup.cfg mcp-process.json ./

# Copy entrypoint
COPY entrypoint.sh ./
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

# Keep the project tarball in the image as a one-time seed.
# entrypoint.sh extracts it into the /app/data/project volume only on the
# first deploy (when the volume is empty) and then symlinks /project to it,
# so user changes under /project persist across deploys.
COPY project.tar.gz /app/project-seed.tar.gz

# Create non-root user. /project is created and chowned in entrypoint.sh
# (after the volume-backed symlink is set up).
RUN useradd -m -s /bin/bash claude && \
    mkdir -p /app/data && \
    chown -R claude:claude /app

# Allow `claude` user to run CLI updates (and only those) as root,
# so the bot's /update command can refresh the global npm package.
RUN printf 'claude ALL=(root) NOPASSWD: /usr/bin/npm install -g @anthropic-ai/claude-code@latest\nclaude ALL=(root) NOPASSWD: /usr/bin/npm install -g @anthropic-ai/claude-code\nclaude ALL=(root) NOPASSWD: /usr/bin/npm install -g @openai/codex@latest\nclaude ALL=(root) NOPASSWD: /usr/bin/npm install -g @openai/codex\n' \
        > /etc/sudoers.d/claude-npm-update && \
    chmod 0440 /etc/sudoers.d/claude-npm-update

# Entrypoint runs as root to fix volume permissions, then drops to claude user
CMD ["./entrypoint.sh"]
