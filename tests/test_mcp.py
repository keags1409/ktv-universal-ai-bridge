"""End-to-end tests: real server processes over stdio, sharing one database file."""
import anyio
import pytest

from conftest import call

pytestmark = pytest.mark.anyio

TOOLS = {
    "bridge_health", "list_projects", "create_project", "save_memory", "search_memory", "get_project_context",
    "create_task", "list_tasks", "claim_next_task", "update_task", "request_agent_help", "submit_handover",
    "get_agent_activity",
}


async def test_all_tools_are_discoverable(connect):
    async with connect() as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == TOOLS
    assert "agent_id" in tools["claim_next_task"].input_schema["required"]


async def test_memory_survives_server_restart(connect):
    async with connect() as c:
        await call(c, "create_project", agent_id="a", name="Persist")
        await call(c, "save_memory", agent_id="a", project="Persist", content="Remember the blue theme")
    async with connect() as c:  # a brand new server process
        found = await call(c, "search_memory", query="blue theme")
    assert found["results"][0]["content"] == "Remember the blue theme"


async def test_two_clients_share_a_project(connect):
    async with connect() as a, connect() as b:
        await call(a, "create_project", agent_id="claude", name="Shared")
        await call(a, "save_memory", agent_id="claude", project="Shared", content="API lives at /v1", kind="fact")
        projects = await call(b, "list_projects")
        ctx = await call(b, "get_project_context", project="Shared")
    assert [p["name"] for p in projects["projects"]] == ["Shared"]
    assert ctx["recent_memories"][0]["content"] == "API lives at /v1"


async def test_two_clients_cannot_claim_the_same_task(connect):
    async with connect() as a, connect() as b:
        await call(a, "create_project", agent_id="a", name="Race")
        await call(a, "create_task", agent_id="a", project="Race", title="only task")
        results = {}

        async def claim(client, agent):
            results[agent] = await call(client, "claim_next_task", agent_id=agent)

        async with anyio.create_task_group() as tg:
            tg.start_soon(claim, a, "agent-a")
            tg.start_soon(claim, b, "agent-b")
    winners = [agent for agent, r in results.items() if r["task"]]
    assert len(winners) == 1


async def test_handover_round_trip(connect):
    async with connect() as a, connect() as b:
        await call(a, "create_project", agent_id="a", name="Hand")
        task = (await call(a, "create_task", agent_id="a", project="Hand", title="t"))["task"]
        await call(a, "claim_next_task", agent_id="a")
        await call(a, "submit_handover", agent_id="a", project="Hand", task_id=task["id"],
                   summary="Done the models", next_steps="Wire the routes")
        claimed = await call(b, "claim_next_task", agent_id="b")
    assert claimed["task"]["id"] == task["id"]
    assert claimed["handovers"][0]["next_steps"] == "Wire the routes"


@pytest.mark.parametrize("tool,args", [
    ("create_project", {"agent_id": "bad id!", "name": "x"}),
    ("create_project", {"agent_id": "a", "name": ""}),
    ("create_task", {"agent_id": "a", "project": "x", "title": "t", "priority": 9}),
    ("create_task", {"agent_id": "a", "project": "x", "title": "t", "sensitive_actions": ["launch_missiles"]}),
    ("update_task", {"agent_id": "a", "task_id": "not-a-task-id"}),
    ("save_memory", {"agent_id": "a", "project": "x"}),  # missing content
    ("search_memory", {"query": "x", "limit": 1000}),
])
async def test_invalid_input_is_rejected(connect, tool, args):
    async with connect() as c:
        r = await c.call_tool(tool, args)
    assert r.is_error


async def test_unauthorised_actions_are_blocked(connect):
    async with connect() as a, connect() as b:
        await call(a, "create_project", agent_id="a", name="Guard")
        risky = (await call(a, "create_task", agent_id="a", project="Guard", title="Deploy",
                            sensitive_actions=["publish_or_deploy"]))["task"]
        assert risky["status"] == "awaiting_approval"
        assert (await call(b, "claim_next_task", agent_id="b"))["task"] is None

        normal = (await call(a, "create_task", agent_id="a", project="Guard", title="Code"))["task"]
        await call(a, "claim_next_task", agent_id="a")
        r = await b.call_tool("update_task", {"agent_id": "b", "task_id": normal["id"], "status": "done"})
        assert r.is_error and "not leased to b" in r.content[0].text
        r = await b.call_tool("update_task", {"agent_id": "b", "task_id": risky["id"], "status": "in_progress"})
        assert r.is_error
        ctx = await call(b, "get_project_context", project="Guard")
        refused = (await call(a, "get_agent_activity", agent_id="b"))["activity"]
    assert ctx["pending_approvals"][0]["action"] == "publish_or_deploy"
    # Refused attempts leave an audit trail, tied to the task and project they targeted.
    assert [(e["action"], e["task_id"]) for e in refused] == [("refused", risky["id"]), ("refused", normal["id"])]
    assert refused[1]["project_id"] == ctx["project"]["id"]
    assert refused[1]["detail"]["operation"] == "update_task"
    assert "not leased to b" in refused[1]["detail"]["error"]
