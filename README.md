# KTV Universal AI Bridge (Phase 1)

A local MCP server that gives several AI agents one shared memory and one task board.
Each agent connects through its own MCP client. Every client starts its own copy of the server, and all copies share one SQLite file. Because of that, agents see the same projects, memories, tasks and handovers, and two agents can never be given the same task.

Phase 1 is local only: stdio transport, no network listener, no dashboard.

## Setup

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest
```

The database is created on first use at `data\bridge.db` (git-ignored). To put it somewhere else, set `KTV_BRIDGE_DB` (see `.env.example`).

## Connecting Claude Code

`.mcp.json` registers the server as `ktv-bridge` for this folder only (project scope). No global configuration is changed.

The first time you run `claude` in this folder, Claude Code asks you to approve the project server. Approve it, then check:

```powershell
claude mcp get ktv-bridge
```

The command path in `.mcp.json` is absolute. Update it if the folder moves.

On this PC the server is also registered at user scope, so Claude Code sessions in every folder can use it. Both entries run the same command and share the same database. Inside this folder the project entry takes priority. This is how the user-scope entry was added:

```powershell
claude mcp add --scope user ktv-bridge -- C:/Users/keage/Desktop/ktv-universal-ai-bridge/.venv/Scripts/python.exe -m ktv_bridge
```

To remove it: `claude mcp remove ktv-bridge -s user`.

## Tools

| Tool | What it does |
|---|---|
| `bridge_health` | Database path, lease length, record counts |
| `list_projects` / `create_project` | Projects. Creating an existing name returns the existing project |
| `save_memory` | Note, fact, decision or context. Exact duplicates are not stored twice. A `key` keeps one current version plus history |
| `search_memory` | All-words search over content, key and tags, newest first |
| `get_project_context` | Context, decisions, recent memories, unfinished tasks, handovers, pending approvals |
| `create_task` | Priority 1 (most urgent) to 5. Optional capability, parent task (for subtasks) and sensitive actions |
| `list_tasks` | Filter by project, status or assignee |
| `claim_next_task` | Atomic claim of the most urgent task the agent can do |
| `update_task` | Status and progress, by the lease holder only. Every call renews the lease |
| `request_agent_help` | Creates a help subtask other agents can claim |
| `submit_handover` | Leaves a note. Releases the task if the caller holds it |
| `get_agent_activity` | Agents, the tasks they hold, and the audit trail |

## How coordination works

```
awaiting_approval --(human approves)--> open --claim--> claimed --start--> in_progress --> done
                                          ^                |                  |
                                          +---- release / handover / lease expiry -------+
```

- **Atomic claims.** `claim_next_task` runs inside `BEGIN IMMEDIATE`, which takes SQLite's write lock across all processes. Picking a task and assigning it happen as one step.
- **Claimed is not executing.** A claim only reserves the task. The agent must set `in_progress` when it starts, and `done` is only accepted from `in_progress`.
- **Leases.** A claim lasts `KTV_BRIDGE_LEASE_SECONDS` (default 600) and each `update_task` renews it. When a lease expires, the next read or claim returns the task to `open`. Its progress note and attempt count are kept, and a `lease_expired` audit entry is written.
- **No silent overwrites.** Only the current lease holder can update a task. An agent whose lease expired gets an error, so it cannot overwrite the new holder's work.
- **Resume.** If an agent reconnects and calls `claim_next_task` while it still holds a task, it gets that same task back (`resumed: true`). Any handover notes for the task are included.
- **Subtasks.** A parent task cannot be claimed or finished while it has unfinished subtasks. `request_agent_help` creates such a subtask.

## Approvals (human only)

Six actions need approval: spending money, publishing or deploying, sending external messages, deleting important data, modifying production, and merging into protected branches. A task that involves any of them must list them in `sensitive_actions`. The task then waits in `awaiting_approval`, and nobody can claim it.

Approval is deliberately **not** an MCP tool, so an agent cannot approve its own work. A human approves from the command line:

```powershell
.venv\Scripts\python.exe -m ktv_bridge approvals
.venv\Scripts\python.exe -m ktv_bridge approve appr_... --by Keagen
.venv\Scripts\python.exe -m ktv_bridge reject  appr_... --by Keagen --reason "not yet"
```

Phase 1 only records and enforces approvals. No tool performs any of the external actions themselves.

## Layout

```
ktv_bridge/storage.py   Store: SQLite schema and all logic (no MCP imports)
ktv_bridge/server.py    MCP tools: input validation -> Store -> JSON
ktv_bridge/__main__.py  stdio server + human approval CLI
tests/                  pytest: storage rules + real stdio clients
scripts/demo.py         end-to-end walkthrough (uses data/demo.db)
```

Every record has a prefixed UUID (`proj_`, `mem_`, `task_`, ...), UTC timestamps and a project link where one applies. Every write is recorded in `audit_log`. So is every refused call (wrong lease holder, unknown id, approval still pending), as a `refused` entry tied to the task or project it targeted. `get_agent_activity` shows these entries.

## Moving to PostgreSQL

`server.py` only calls the public methods of `Store`. To use a hosted database, write a `PostgresStore` with the same methods and choose it in `_call()`. Most of the SQL carries over. Two parts need changing:

- Replace `BEGIN IMMEDIATE` plus "select then update" in `claim_next_task` with `SELECT ... FOR UPDATE SKIP LOCKED` inside a transaction.
- Replace the `LIKE` search with Postgres full-text search (`tsvector`).

## Remote access (future, not built)

Do **not** expose this server on a network without authentication. The planned route:

1. Run with the Streamable HTTP transport: `mcp.run("streamable-http", ...)`. Put it behind HTTPS only.
2. Make the server an OAuth 2.1 resource server, as the MCP authorization spec describes. The SDK's `MCPServer` accepts `auth=AuthSettings(issuer_url=..., resource_server_url=..., required_scopes=[...])` and a `token_verifier`. Use an existing identity provider as the authorization server rather than writing one.
3. Take `agent_id` from the verified token, not from tool arguments, so agents cannot pretend to be each other. Give each user or agent its own scopes.
4. Move storage to PostgreSQL (above) so several server instances can share it.
5. Keep approvals out of the MCP surface. If approvals move online, they need a separate human-only, authenticated route.

Client notes (check each vendor's current documentation before relying on them):

- **Claude Code / Claude Desktop** can already start local stdio servers like this one.
- **claude.ai and ChatGPT** connect to remote MCP servers over HTTPS with OAuth. They need step 1–3 first.
- **Antigravity** reads local stdio servers from `%USERPROFILE%\.gemini\config\mcp_config.json`. A `ktv-bridge` entry with the same command as `.mcp.json` is set up there (backup: `mcp_config.json.bak-ktv-bridge`). Both apps share `data\bridge.db`. Use `agent_id: "antigravity"` there.

## Known limitations

- Search is a word-match `LIKE` scan, not ranked or semantic.
- An agent holds one task at a time per project filter (`claim_next_task` returns the held task first).
- Lease expiry is lazy: it happens on the next read or claim, not on a timer.
- Agent identity is self-declared via `agent_id`. That is fine on one machine, but remote use needs authenticated identity.
- `.mcp.json` uses an absolute Windows path.
- Calls rejected by input-schema validation (bad `agent_id` format, out-of-range values) are refused by the MCP SDK before the tool runs, so they are not audited.
