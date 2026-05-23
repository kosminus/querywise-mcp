"""Application settings for querywise-mcp.

Loaded from environment variables (and an optional .env file). All values
have sensible defaults so the server runs out of the box with zero config
for keyword-only operation; an LLM/embedding key unlocks the full pipeline.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Default data directory: ~/.querywise (override with QUERYWISE_HOME).
_DEFAULT_HOME = Path.home() / ".querywise"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_name: str = "querywise-mcp"
    environment: str = "development"
    debug: bool = False

    # Where the SQLite metadata store + any local artifacts live.
    home_dir: Path = _DEFAULT_HOME

    # App metadata store (SQLite + sqlite-vec). If unset, derived from home_dir.
    database_url: str = ""

    # Security — Fernet key for encrypting stored target-DB connection strings.
    encryption_key: str = "dev-encryption-key-change-in-production"

    # Query defaults
    default_query_timeout_seconds: int = 30
    default_max_rows: int = 1000
    max_retry_attempts: int = 3

    # LLM defaults (only needed for the `ask`/CLI pipeline and cloud embeddings)
    default_llm_provider: str = "anthropic"
    default_llm_model: str = "claude-sonnet-4-20250514"
    embedding_model: str = "text-embedding-3-small"

    # API keys — read from env or .env (OPENAI_API_KEY / ANTHROPIC_API_KEY).
    # The SDKs read os.environ, which .env does NOT populate, so we load the
    # keys here and pass them explicitly to the providers.
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    # Ollama settings (used when default_llm_provider = "ollama")
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_embedding_model: str = "nomic-embed-text"

    # Context builder
    max_context_tables: int = 8
    max_sample_queries: int = 3
    embedding_dimension: int = 1536

    # MCP transport (used by `querywise serve` / the server entry point)
    mcp_transport: str = "stdio"  # "stdio" | "http"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8077

    # Sample DB seeding (opt-in via the CLI `seed-sample` command).
    # Defaults to a zero-infra local SQLite file built by services.sample_data.
    # For sqlite, leave sample_db_connection_string blank to use the default path;
    # any non-sqlite value here is ignored when sample_db_connector_type=="sqlite".
    sample_db_connector_type: str = "sqlite"
    sample_db_connection_string: str = ""

    def resolved_database_url(self) -> str:
        """Async SQLAlchemy URL for the metadata store."""
        if self.database_url:
            return self.database_url
        self.home_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{self.home_dir / 'querywise.db'}"

    def resolved_sample_db_target(self) -> str:
        """Connection string for the sample target database.

        For sqlite (default): the path to the local sample file (built on demand),
        ignoring any Postgres-style value left in sample_db_connection_string.
        """
        if self.sample_db_connector_type == "sqlite":
            cs = self.sample_db_connection_string
            if cs and cs.endswith(".db"):
                return cs
            self.home_dir.mkdir(parents=True, exist_ok=True)
            return str(self.home_dir / "sample_ifrs9.db")
        return self.sample_db_connection_string


settings = Settings()
