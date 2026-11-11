"""MCP interface over stdio. Validates input, calls `Store`, returns JSON objects."""
from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .storage import MEMORY_KINDS, SENSITIVE_ACTIONS, TASK_STATUSES, BridgeError, Store, default_db_path

AgentId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
                               description="Your stable agent name, e.g. 'claude-code', 'chatgpt', 'antigravity'.")]
ProjectRef = Annotated[str, Field(min_length=1, max_length=120, description="Project id or exact project name.")]
TaskId = Annotated[str, Field(pattern=r"^task_[0-9a-f]{32}$")]
Capability = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$", description="e.g. 'python', 'design'.")]
Tag = Annotated[str, Field(min_length=1, max_length=40)]
Priority = Annotated[int, Field(ge=1, le=5, description="1 = most urgent, 5 = least urgent.")]
LeaseSeconds = Annotated[int, Field(ge=60, le=86400, description="Lease length; defaults to the server setting.")]
Limit = Annotated[int, Field(ge=1, le=200)]

mcp = MCPServer(
    "ktv-universal-ai-bridge",
    instructions=(
        "Shared memory and task board for several AI agents. Always pass the same agent_id. "
        "Start with get_project_context. To work: claim_next_task -> update_task(status='in_progress') -> "
        "update_task(progress_note=...) as you go -> update_task(status='done'), or submit_handover if you stop. "
        "Tasks that spend money, publish/deploy, send external messages, delete important data, touch "
        "production or merge protected branches must list sensitive_actions and wait for human approval."
    ),
)

log = logging.getLogger(__name__)
_store: Store | None = None


def _call(method: str, **kwargs: Any) -> dict[str, Any]:
    global _store
    if _store is None:
        _store = Store(default_db_path())
    try:
        return getattr(_store, method)(**kwargs)
    except BridgeError as e:
        try:
            _store.log_refusal(kwargs.get("agent_id"), method, str(e), kwargs.get("project"), kwargs.get("task_id"))
        except Exception:  # never hide the real refusal behind a logging failure
            log.exception("could not audit refused %s call", method)
        raise ToolError(str(e)) from None


@mcp.tool()
def bridge_health() -> dict[str, Any]:
    """Check the bridge is up; returns database path, lease length and record counts."""
    return _call("health")


@mcp.tool()
def list_projects() -> dict[str, Any]:
    """List all projects with their number of unfinished tasks."""
    return _call("list_projects")


@mcp.tool()
def create_project(
    agent_id: AgentId,
    name: Annotated[str, Field(min_length=1, max_length=120)],
    description: Annotated[str, Field(max_length=2000)] = "",
) -> dict[str, Any]:
    """Create a project. If one with the same name exists it is returned with created=false."""
    return _call("create_project", agent_id=agent_id, name=name, description=description)


@mcp.tool()
def save_memory(
    agent_id: AgentId,
    project: ProjectRef,
    content: Annotated[str, Field(min_length=1, max_length=20000)],
    kind: Literal[MEMORY_KINDS] = "note",
    key: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}$")] | None = None,
    tags: Annotated[list[Tag], Field(max_length=20)] | None = None,
) -> dict[str, Any]:
    """Save shared memory for a project.

    kind: 'decision' for decisions, 'context' for standing project context, 'fact' or 'note' otherwise.
    key: optional stable name (e.g. 'tech-stack'). Saving the same key again stores a new version and keeps
    the old one as history. Exact duplicates are not stored twice; the existing record comes back with
    duplicate=true.
    """
    return _call("save_memory", agent_id=agent_id, project=project, content=content, kind=kind, key=key, tags=tags)


@mcp.tool()
def search_memory(
    query: Annotated[str, Field(min_length=1, max_length=500, description="Words that must all appear.")],
    project: ProjectRef | None = None,
    kind: Literal[MEMORY_KINDS] | None = None,
    limit: Limit = 20,
    include_history: bool = False,
) -> dict[str, Any]:
    """Search memories (content, key and tags), newest first. Old versions only with include_history=true."""
    return _call("search_memory", query=query, project=project, kind=kind, limit=limit,
                 include_history=include_history)


@mcp.tool()
def get_project_context(project: ProjectRef) -> dict[str, Any]:
    """Everything an agent needs to start: context, decisions, recent memories, unfinished tasks,
    recent handovers and approvals still waiting for a human."""
    return _call("project_context", project=project)


@mcp.tool()
def create_task(
    agent_id: AgentId,
    project: ProjectRef,
    title: Annotated[str, Field(min_length=1, max_length=200)],
    description: Annotated[str, Field(max_length=10000)] = "",
    priority: Priority = 3,
    required_capability: Capability | None = None,
    parent_task_id: TaskId | None = None,
    sensitive_actions: Annotated[list[Literal[SENSITIVE_ACTIONS]], Field(max_length=6)] | None = None,
) -> dict[str, Any]:
    """Add a task to the board. Use parent_task_id to split a large task into subtasks; the parent cannot be
    claimed or finished until its subtasks are done or cancelled.

    A task that would spend money, publish or deploy, send external messages, delete important data, modify
    production or merge into a protected branch MUST list those in sensitive_actions. It then waits in
    'awaiting_approval' and nobody can claim it until a human approves it outside MCP.
    """
    return _call("create_task", agent_id=agent_id, project=project, title=title, description=description,
                 priority=priority, required_capability=required_capability, parent_task_id=parent_task_id,
                 sensitive_actions=sensitive_actions)


@mcp.tool()
def list_tasks(
    project: ProjectRef | None = None,
    status: Literal[TASK_STATUSES] | None = None,
    agent_id: Annotated[AgentId | None, Field(description="Only tasks assigned to this agent.")] = None,
    limit: Limit = 50,
) -> dict[str, Any]:
    """List tasks, most urgent first. Expired leases are recovered (task back to 'open') before listing."""
    return _call("list_tasks", project=project, status=status, agent_id=agent_id, limit=limit)


@mcp.tool()
def claim_next_task(
    agent_id: AgentId,
    capabilities: Annotated[list[Capability], Field(max_length=30)] | None = None,
    project: ProjectRef | None = None,
    lease_seconds: LeaseSeconds | None = None,
) -> dict[str, Any]:
    """Atomically claim the most urgent open task you can do; two agents can never get the same task.

    If you already hold a task it is returned again (resumed=true) so you can continue it. Capabilities you
    pass are remembered. A claim only reserves the task: call update_task(status='in_progress') when you
    actually start, and call update_task again before lease_expires_at or the task returns to the queue.
    Recent handovers for the task are included.
    """
    return _call("claim_next_task", agent_id=agent_id, capabilities=capabilities, project=project,
                 lease_seconds=lease_seconds)


@mcp.tool()
def update_task(
    agent_id: AgentId,
    task_id: TaskId,
    status: Literal["in_progress", "blocked", "done", "open", "cancelled"] | None = None,
    progress_note: Annotated[str, Field(max_length=5000)] | None = None,
    lease_seconds: LeaseSeconds | None = None,
) -> dict[str, Any]:
    """Record progress or change status on a task you hold. Every call renews your lease.

    Only the current lease holder may update a task. status='open' releases it, 'done' requires the task to be
    'in_progress' first and all subtasks finished.
    """
    return _call("update_task", agent_id=agent_id, task_id=task_id, status=status, progress_note=progress_note,
                 lease_seconds=lease_seconds)


@mcp.tool()
def request_agent_help(
    agent_id: AgentId,
    task_id: TaskId,
    request: Annotated[str, Field(min_length=1, max_length=2000)],
    capability: Capability | None = None,
    priority: Priority | None = None,
) -> dict[str, Any]:
    """Ask other agents for help: creates a subtask of task_id that any agent (with `capability`, if given)
    can claim."""
    return _call("request_agent_help", agent_id=agent_id, task_id=task_id, request=request, capability=capability,
                 priority=priority)


@mcp.tool()
def submit_handover(
    agent_id: AgentId,
    project: ProjectRef,
    summary: Annotated[str, Field(min_length=1, max_length=10000, description="What was done and the state now.")],
    next_steps: Annotated[str, Field(max_length=10000)] = "",
    task_id: TaskId | None = None,
    to_agent: AgentId | None = None,
) -> dict[str, Any]:
    """Leave a handover note. If task_id is a task you hold, it is released back to the queue so another agent
    (or you, later) can pick it up; the note is shown to whoever claims it next."""
    return _call("submit_handover", agent_id=agent_id, project=project, summary=summary, next_steps=next_steps,
                 task_id=task_id, to_agent=to_agent)


@mcp.tool()
def get_agent_activity(
    agent_id: AgentId | None = None,
    project: ProjectRef | None = None,
    limit: Limit = 50,
) -> dict[str, Any]:
    """Known agents (capabilities, last seen, tasks they hold) and the audit trail, newest first."""
    return _call("agent_activity", agent_id=agent_id, project=project, limit=limit)


# Approving sensitive work is deliberately not a tool: only a human can do it, with `python -m ktv_bridge approve`.
