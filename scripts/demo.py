"""Phase 1 walkthrough over real stdio MCP connections, using data/demo.db.

Run: .venv\\Scripts\\python.exe scripts\\demo.py
"""
import sys
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

DB = Path(__file__).resolve().parent.parent / "data" / "demo.db"


def server():
    return Client(StdioServerParameters(command=sys.executable, args=["-m", "ktv_bridge"],
                                        env={"KTV_BRIDGE_DB": str(DB)}))


async def call(client, tool, **args):
    r = await client.call_tool(tool, args)
    if r.is_error:
        raise SystemExit(f"{tool} failed: {r.content[0].text}")
    return r.structured_content


async def main():
    for suffix in ("", "-wal", "-shm"):  # start from an empty demo database
        Path(f"{DB}{suffix}").unlink(missing_ok=True)

    async with server() as a, server() as b:  # two independent server processes, one per agent
        p = (await call(a, "create_project", agent_id="agent-a", name="Demo Site",
                        description="Phase 1 demo"))["project"]
        print(f"1. created project {p['name']} ({p['id']})")
        m = (await call(a, "save_memory", agent_id="agent-a", project="Demo Site", kind="decision",
                        key="stack", content="Build the site with plain HTML and Tailwind"))["memory"]
        print(f"2. saved memory v{m['version']}: {m['content']}")
        for title, prio in (("Write homepage copy", 1), ("Build contact form", 2), ("Optimise images", 3)):
            t = (await call(a, "create_task", agent_id="agent-a", project="Demo Site", title=title,
                            priority=prio))["task"]
            print(f"3. created task P{prio} {t['title']}")
        ta = (await call(a, "claim_next_task", agent_id="agent-a"))["task"]
        tb = (await call(b, "claim_next_task", agent_id="agent-b"))["task"]
        print(f"4. agent-a claimed '{ta['title']}', agent-b claimed '{tb['title']}'")
        await call(a, "update_task", agent_id="agent-a", task_id=ta["id"], status="in_progress",
                   progress_note="Hero and services sections drafted")
        await call(b, "update_task", agent_id="agent-b", task_id=tb["id"], status="in_progress",
                   progress_note="Form markup done, validation next")
        print("5. both agents recorded progress (status in_progress)")
        h = await call(b, "submit_handover", agent_id="agent-b", project="Demo Site", task_id=tb["id"],
                       summary="Form markup done", next_steps="Add validation and spam protection")
        print(f"6. agent-b handed over; task released={h['task_released']}")

    print("7. both servers stopped; starting a fresh one")
    async with server() as c:
        ctx = await call(c, "get_project_context", project="Demo Site")
        print(f"8. after restart: {len(ctx['decisions'])} decision(s): {ctx['decisions'][0]['content']}")
        for t in ctx["unfinished_tasks"]:
            print(f"   task {t['status']:<11} {t['assigned_to'] or '-':<8} {t['title']} | {t['progress']}")
        print(f"   latest handover: {ctx['recent_handovers'][0]['next_steps']}")
        resumed = (await call(c, "claim_next_task", agent_id="agent-a"))
        print(f"   agent-a reconnects and resumes: '{resumed['task']['title']}' (resumed={resumed['resumed']})")


if __name__ == "__main__":
    anyio.run(main)
