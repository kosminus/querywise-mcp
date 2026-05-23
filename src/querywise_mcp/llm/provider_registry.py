from querywise_mcp.llm.base_provider import BaseLLMProvider, LLMProviderType

_PROVIDER_CLASSES: dict[LLMProviderType, type[BaseLLMProvider]] = {}
_instances: dict[str, BaseLLMProvider] = {}


def _register_defaults() -> None:
    """Lazily register built-in providers.

    Each provider imports independently so the Ollama-only path (no cloud SDKs
    installed) still works when ``anthropic``/``openai`` are absent.
    """
    if _PROVIDER_CLASSES:
        return
    try:
        from querywise_mcp.llm.providers.anthropic_provider import AnthropicProvider

        _PROVIDER_CLASSES[LLMProviderType.ANTHROPIC] = AnthropicProvider
    except ImportError:
        pass
    try:
        from querywise_mcp.llm.providers.openai_provider import OpenAIProvider

        _PROVIDER_CLASSES[LLMProviderType.OPENAI] = OpenAIProvider
    except ImportError:
        pass
    try:
        from querywise_mcp.llm.providers.ollama_provider import OllamaProvider

        _PROVIDER_CLASSES[LLMProviderType.OLLAMA] = OllamaProvider
    except ImportError:
        pass


def register_provider(provider_type: LLMProviderType, cls: type[BaseLLMProvider]) -> None:
    _PROVIDER_CLASSES[provider_type] = cls


def get_provider(provider_type: str, api_key: str | None = None) -> BaseLLMProvider:
    """Get or create a provider instance.

    When no api_key is passed, fall back to the key configured in settings
    (loaded from env / .env). This is required because the cloud SDKs read
    os.environ for their key, which a .env file does NOT populate.
    """
    _register_defaults()

    try:
        pt = LLMProviderType(provider_type)
    except ValueError:
        raise ValueError(
            f"Unknown provider: {provider_type}. "
            f"Available: {[t.value for t in LLMProviderType]}"
        )

    if api_key is None:
        from querywise_mcp.config import settings

        if pt == LLMProviderType.OPENAI:
            api_key = settings.openai_api_key
        elif pt == LLMProviderType.ANTHROPIC:
            api_key = settings.anthropic_api_key

    cache_key = f"{provider_type}:{api_key or 'default'}"
    if cache_key in _instances:
        return _instances[cache_key]

    cls = _PROVIDER_CLASSES.get(pt)
    if cls is None:
        raise ValueError(f"Provider '{provider_type}' is not registered.")

    instance = cls(api_key=api_key) if api_key else cls()
    _instances[cache_key] = instance
    return instance


def get_embedding_provider(api_key: str | None = None) -> BaseLLMProvider:
    """Get a provider that supports embeddings.

    Uses the configured LLM provider: Ollama embeds locally, OpenAI uses
    text-embedding-3-small, Anthropic falls back to OpenAI.
    """
    from querywise_mcp.config import settings

    provider_type = settings.default_llm_provider

    # Anthropic doesn't support embeddings — fall back to OpenAI
    if provider_type == "anthropic":
        provider_type = "openai"

    return get_provider(provider_type, api_key=api_key)
