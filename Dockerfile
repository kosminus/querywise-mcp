# querywise-mcp — MCP server over stdio.
#
# Build:  docker build -t querywise-mcp .
# Run:    docker run -i --rm -v querywise-data:/data querywise-mcp
#         (MCP clients / Glama launch the container and speak JSON-RPC on stdio)
#
# The image installs the core package (SQLite + PostgreSQL targets, embedded
# SQLite + sqlite-vec metadata store). For the LLM-powered `ask` pipeline or
# cloud embeddings, install an extra and pass a key, e.g.:
#   RUN pip install --no-cache-dir ".[llm]"
#   docker run -i --rm -e DEFAULT_LLM_PROVIDER=openai -e OPENAI_API_KEY=... ...
FROM python:3.11-slim

WORKDIR /app

# Install dependencies from metadata first for better layer caching.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Writable location for the SQLite + sqlite-vec metadata store. init_db() runs
# on startup (server lifespan) and creates the DB here.
ENV HOME_DIR=/data
RUN mkdir -p /data
VOLUME ["/data"]

# Launch the MCP server over stdio — this is what an MCP client connects to.
ENTRYPOINT ["querywise-mcp"]
