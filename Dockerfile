FROM python:3.11-slim

# Install Node.js 20.x (required by claude-agent-sdk) + system deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl git ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install Poetry 2.x
RUN pip install --no-cache-dir "poetry>=2.0"

WORKDIR /app

# Copy dependency files first (Docker cache layer)
COPY pyproject.toml poetry.lock setup.cfg ./

# Install production dependencies only (no virtualenv in container)
RUN poetry config virtualenvs.create false && \
    poetry install --only main --no-interaction --no-ansi

# Copy source code
COPY src/ ./src/

# Copy entrypoint
COPY entrypoint.sh ./
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

# Create non-root user
RUN useradd -m -s /bin/bash claude && \
    mkdir -p /project /app/data && \
    chown -R claude:claude /app /project

USER claude

CMD ["./entrypoint.sh"]
