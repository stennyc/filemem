"""File-memory provider config schema."""

from plugins.memory.config_schema import (
    KIND_BOOL,
    KIND_NUMBER,
    ProviderConfigSchema,
    ProviderField,
)



CONFIG_SCHEMA = ProviderConfigSchema(
    name="filemem",
    label="Filemem",
    storage="flat_json",
    docs_url="https://hermes-agent.nousresearch.com/docs/plugins/filemem",
    fields=(
        # Connection
        ProviderField(
            key="auto_extract",
            label="Auto Extract",
            kind=KIND_BOOL,
            default="true",
            description="Automatically extract memories from conversations",
            inline=True,
            group="Behavior",
        ),
        ProviderField(
            key="min_search_score",
            label="Min Search Score",
            kind=KIND_NUMBER,
            default="0.3",
            description="Relevance gate for keyword-overlap × decay scores (range [0,1], NOT cosine). 0.3 filters ~23% of multi-word queries (data-backed); raise toward 0.4 only if auto-injects are too loose. 0.5+ kills most recall.",
            inline=True,
            group="Behavior",
        ),
    ),
)
