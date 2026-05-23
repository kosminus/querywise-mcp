# QueryWise MCP Architecture

This document shows the high-level architecture for `querywise-mcp`: a headless
MCP server and CLI that ground natural-language database questions in cached
schema and semantic metadata, then execute read-only SQL through target
connectors.

## Component Diagram

```mermaid
flowchart LR
    user["User / MCP client LLM"]
    cli["CLI<br/>querywise"]
    server["MCP server<br/>server.py"]

    subgraph tools["Tool Surface"]
        connections["Connection tools"]
        schema["Schema tools"]
        semantic["Semantic-layer tools"]
        query["Query tools<br/>get_semantic_context<br/>run_sql<br/>generate_sql<br/>ask"]
    end

    subgraph services["Application Services"]
        connection_service["connection_service<br/>create, encrypt, resolve, test"]
        schema_service["schema_service<br/>introspect and cache"]
        semantic_service["semantic_service<br/>glossary, metrics, dictionary, samples"]
        knowledge_service["knowledge_service<br/>document import and chunks"]
        setup_service["setup_service<br/>sample data and embeddings"]
        query_service["query_service<br/>NL to SQL pipeline and history"]
    end

    subgraph semantic_layer["Semantic Context"]
        context_builder["context_builder<br/>question-aware context"]
        schema_linker["schema_linker"]
        glossary_resolver["glossary_resolver"]
        relevance_scorer["relevance_scorer"]
        prompt_assembler["prompt_assembler"]
    end

    subgraph llm["LLM Pipeline"]
        router["router"]
        composer["QueryComposerAgent"]
        validator["SQLValidatorAgent"]
        error_handler["ErrorHandlerAgent"]
        interpreter["ResultInterpreterAgent"]
        providers["Providers<br/>Anthropic / OpenAI / Ollama"]
    end

    subgraph db["Metadata Store<br/>SQLite + sqlite-vec"]
        connections_db[("database_connections")]
        schema_db[("cached_tables<br/>cached_columns<br/>cached_relationships")]
        semantic_db[("glossary<br/>metrics<br/>dictionary<br/>sample_queries<br/>knowledge")]
        history_db[("query_history")]
    end

    subgraph connectors["Target Database Connectors"]
        registry["connector_registry"]
        postgres["PostgreSQL"]
        sqlite["SQLite"]
        bigquery["BigQuery"]
        databricks["Databricks"]
    end

    targets[("User databases")]

    user --> server
    cli --> services
    server --> tools
    tools --> services

    connection_service --> connections_db
    schema_service --> schema_db
    semantic_service --> semantic_db
    knowledge_service --> semantic_db
    setup_service --> semantic_db
    query_service --> history_db

    schema_service --> registry
    query_service --> registry
    registry --> postgres
    registry --> sqlite
    registry --> bigquery
    registry --> databricks
    postgres --> targets
    sqlite --> targets
    bigquery --> targets
    databricks --> targets

    query_service --> context_builder
    query --> context_builder
    context_builder --> schema_linker
    context_builder --> glossary_resolver
    context_builder --> relevance_scorer
    context_builder --> prompt_assembler
    context_builder --> schema_db
    context_builder --> semantic_db

    query_service --> router
    router --> providers
    query_service --> composer
    query_service --> validator
    query_service --> error_handler
    query_service --> interpreter
    composer --> providers
    error_handler --> providers
    interpreter --> providers
```

## Main Query Sequence

```mermaid
sequenceDiagram
    autonumber
    actor Client as MCP client / CLI
    participant Server as server.py / cli.py
    participant QueryService as query_service
    participant Context as semantic.context_builder
    participant Store as SQLite metadata store
    participant LLM as LLM agents + provider
    participant Registry as connector_registry
    participant DB as Target database

    alt Thin MCP path
        Client->>Server: get_semantic_context(connection, question)
        Server->>Context: build_context(connection_id, question)
        Context->>Store: load relevant schema and semantic metadata
        Store-->>Context: tables, glossary, metrics, examples, knowledge
        Context-->>Server: prompt_context
        Server-->>Client: grounded context
        Client->>Server: run_sql(connection, sql)
        Server->>QueryService: execute_raw_sql(connection_id, sql)
    else Thick server-side path
        Client->>Server: ask(connection, question)
        Server->>QueryService: execute_nl_query(connection_id, question)
        QueryService->>Context: build_context(connection_id, question)
        Context->>Store: load relevant schema and semantic metadata
        Store-->>Context: context inputs
        Context-->>QueryService: prompt_context
        QueryService->>LLM: compose SQL
        LLM-->>QueryService: generated SQL
        QueryService->>LLM: validate / repair when needed
    end

    QueryService->>Registry: get_or_create_connector(connection_id)
    Registry-->>QueryService: connector
    QueryService->>DB: execute read-only query
    DB-->>QueryService: rows and metadata
    QueryService->>Store: save query execution history
    QueryService-->>Server: result
    Server-->>Client: rows or Markdown answer
```

## Deployment View

```mermaid
flowchart TB
    subgraph local["Local machine"]
        mcp_client["MCP client<br/>Claude, Cursor, Claude Code"]
        querywise["querywise-mcp process<br/>stdio or HTTP"]
        metadata[("~/.querywise/querywise.db<br/>metadata store")]
        sample_db[("optional sample SQLite DB")]
    end

    subgraph external["External services"]
        llm_api["Optional LLM / embedding APIs<br/>Anthropic, OpenAI, Ollama"]
        target_db["Target databases<br/>Postgres, BigQuery, Databricks, SQLite"]
    end

    mcp_client <-->|MCP tools/resources/prompts| querywise
    querywise <-->|SQLAlchemy / sqlite-vec| metadata
    querywise <-->|optional local SQL| sample_db
    querywise <-->|LLM and embeddings| llm_api
    querywise <-->|read-only queries and introspection| target_db
```
