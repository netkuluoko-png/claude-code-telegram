FROM python:3.11-slim

# Install Node.js 20.x (required by claude-agent-sdk) + system deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl git ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install Poetry
RUN pip install --no-cache-dir poetry

WORKDIR /app

# Copy dependency files first (Docker cache layer)
COPY pyproject.toml poetry.lock ./

# Install production dependencies only
RUN poetry config virtualenvs.create false && \
    poetry install --no-dev --no-interaction --no-ansi

# Copy source code
COPY src/ ./src/
COPY config/ ./config/

# Copy entrypoint
COPY entrypoint.sh ./
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

# Create non-root user
RUN useradd -m -s /bin/bash claude && \
    mkdir -p /project /app/data && \
    chown -R claude:claude /app /project

USER claude

# Create data dir for SQLite
RUN mkdir -p /app/data

CMD ["./entrypoint.sh"]
