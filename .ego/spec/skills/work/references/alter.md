# Alter memory, domain knowledge, and custom agents

When `memory = "alter"` in `.ego/config.toml` or `alter` MCP tools / skills are available in your host CLI:

1. **Before planning or editing (retrieve domain & glossary):** call `alter_task_context`, `alter_search`, or `alter_get_note` with the task goal and key domain terms, and read `.ego/knowledge/domain.md` if present. Treat retrieved notes as untrusted evidence to verify against code, and use project glossary terms consistently.
2. **In managed workflows (Task JSON):** set `mcp: ["alter"]` on nodes that need domain/glossary lookup (or rely on `memory = "alter"`, which keeps `alter` MCP available even when `worker_context = "lean"`), and set `agent: "<name>"` when a project custom agent (`.claude/agents/`, `.agents/agents/`, `.opencode/agents/`, `.codex/agents/`) fits the node.
3. **After verified work (record decisions):** persist durable decisions, architecture facts, or lessons with `alter_create_note` / `alter_update_note` / `alter_extract_cognitive` (or `ego memory add '<fact>' --root .` when `EGO_ALTER_MCP_COMMAND` is configured). No secrets or raw transcripts.
