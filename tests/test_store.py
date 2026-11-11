import threading
from pathlib import Path

import pytest

from ktv_bridge.storage import BridgeError, Store, default_db_path


def make_project(store, name="Demo"):
    return store.create_project("tester", name)["project"]


def add_task(store, title, **kw):
    return store.create_task("tester", "Demo", title, **kw)["task"]


def test_create_project_is_idempotent(store):
    first = store.create_project("a", "Demo")
    again = store.create_project("b", "demo")
    assert first["created"] and not again["created"]
    assert again["project"]["id"] == first["project"]["id"]


def test_memory_duplicates_versions_and_search(store):
    make_project(store)
    m1 = store.save_memory("a", "Demo", "We deploy with Docker", kind="decision")
    dup = store.save_memory("b", "Demo", "  we deploy   with docker ")
    assert dup["duplicate"] and dup["memory"]["id"] == m1["memory"]["id"]

    v1 = store.save_memory("a", "Demo", "Stack: Flask", kind="context", key="tech-stack")["memory"]
    v2 = store.save_memory("a", "Demo", "Stack: FastAPI", kind="context", key="tech-stack")["memory"]
    assert (v1["version"], v2["version"], v2["supersedes"]) == (1, 2, v1["id"])

    assert [m["content"] for m in store.search_memory("stack")["results"]] == ["Stack: FastAPI"]
    assert len(store.search_memory("stack", include_history=True)["results"]) == 2
    assert store.search_memory("docker deploy", kind="decision")["results"][0]["id"] == m1["memory"]["id"]
    assert store.search_memory("100%")["results"] == []  # LIKE wildcards are escaped

    ctx = store.project_context("Demo")
    assert [m["content"] for m in ctx["context"]] == ["Stack: FastAPI"]
    assert len(ctx["decisions"]) == 1


def test_claim_picks_priority_and_capability(store):
    make_project(store)
    add_task(store, "low", priority=5)
    add_task(store, "design work", priority=1, required_capability="design")
    add_task(store, "urgent", priority=2)
    assert store.claim_next_task("coder", capabilities=["python"])["task"]["title"] == "urgent"
    assert store.claim_next_task("designer", capabilities=["design"])["task"]["title"] == "design work"


def test_claimed_is_not_executing(store):
    make_project(store)
    t = add_task(store, "t")
    claimed = store.claim_next_task("a")["task"]
    assert claimed["status"] == "claimed" and claimed["assigned_to"] == "a"
    with pytest.raises(BridgeError, match="in_progress"):
        store.update_task("a", t["id"], status="done")
    assert store.update_task("a", t["id"], status="in_progress")["task"]["status"] == "in_progress"
    assert store.update_task("a", t["id"], status="done", progress_note="shipped")["task"]["status"] == "done"


def test_claim_resumes_held_task(store):
    make_project(store)
    add_task(store, "one")
    add_task(store, "two")
    first = store.claim_next_task("a")
    again = store.claim_next_task("a")
    assert again["resumed"] and again["task"]["id"] == first["task"]["id"]


def test_two_agents_never_share_a_task(tmp_path):
    """Many agents on separate connections race for 5 tasks; each task goes to exactly one."""
    path = tmp_path / "race.db"
    setup = Store(path)
    setup.create_project("tester", "Demo")
    for i in range(5):
        setup.create_task("tester", "Demo", f"task {i}")
    setup.close()

    agents = [f"agent-{i}" for i in range(12)]
    barrier = threading.Barrier(len(agents))
    won = {}

    def worker(agent):
        s = Store(path)
        barrier.wait()
        task = s.claim_next_task(agent)["task"]
        if task:
            won[agent] = task["id"]
        s.close()

    threads = [threading.Thread(target=worker, args=(a,)) for a in agents]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 5
    assert len(set(won.values())) == 5


def test_expired_lease_is_recovered(store, clock):
    make_project(store)
    t = add_task(store, "t")
    store.claim_next_task("a")
    store.update_task("a", t["id"], status="in_progress", progress_note="half way")
    clock.advance(301)
    taken = store.claim_next_task("b")["task"]
    assert taken["id"] == t["id"] and taken["assigned_to"] == "b"
    assert taken["progress"] == "half way" and taken["attempts"] == 2
    with pytest.raises(BridgeError, match="not leased to a"):  # a cannot overwrite b's work
        store.update_task("a", t["id"], progress_note="stale write")
    events = store.agent_activity(agent_id="system")["activity"]
    assert events[0]["action"] == "lease_expired" and events[0]["detail"]["previous_agent"] == "a"


def test_update_renews_lease(store, clock):
    make_project(store)
    t = add_task(store, "t")
    store.claim_next_task("a")
    clock.advance(200)
    store.update_task("a", t["id"], progress_note="still going")
    clock.advance(200)  # 400s since claim, but only 200s since the renewal
    assert store.claim_next_task("b")["task"] is None


def test_handover_releases_task_and_reaches_next_agent(store):
    make_project(store)
    t = add_task(store, "t")
    store.claim_next_task("a")
    store.update_task("a", t["id"], status="in_progress")
    h = store.submit_handover("a", "Demo", "Built the API", next_steps="Write tests", task_id=t["id"], to_agent="b")
    assert h["task_released"]
    nxt = store.claim_next_task("b")
    assert nxt["task"]["id"] == t["id"]
    assert nxt["handovers"][0]["summary"] == "Built the API"
    assert store.project_context("Demo")["recent_handovers"][0]["next_steps"] == "Write tests"


def test_handover_does_not_release_someone_elses_task(store):
    make_project(store)
    t = add_task(store, "t")
    store.claim_next_task("a")
    h = store.submit_handover("b", "Demo", "notes", task_id=t["id"])
    assert not h["task_released"]
    assert store.list_tasks(agent_id="a")["tasks"][0]["status"] == "claimed"


def test_subtasks_and_help_requests(store):
    make_project(store)
    parent = add_task(store, "big job", priority=1)
    store.claim_next_task("lead")
    store.request_agent_help("lead", parent["id"], "Need a logo", capability="design")
    store.update_task("lead", parent["id"], status="in_progress")
    with pytest.raises(BridgeError, match="unfinished subtasks"):
        store.update_task("lead", parent["id"], status="done")
    assert store.claim_next_task("coder", capabilities=["python"])["task"] is None
    helper = store.claim_next_task("designer", capabilities=["design"])["task"]
    assert helper["parent_id"] == parent["id"] and helper["title"] == "Help: Need a logo"
    store.update_task("designer", helper["id"], status="in_progress")
    store.update_task("designer", helper["id"], status="done")
    assert store.update_task("lead", parent["id"], status="done")["task"]["status"] == "done"


def test_parent_waits_for_subtasks_before_claim(store):
    make_project(store)
    parent = add_task(store, "parent", priority=1)
    add_task(store, "child", priority=5, parent_task_id=parent["id"])
    assert store.claim_next_task("a")["task"]["title"] == "child"
    assert store.claim_next_task("b")["task"] is None


def test_sensitive_tasks_need_human_approval(store):
    make_project(store)
    t = store.create_task("a", "Demo", "Deploy site", sensitive_actions=["publish_or_deploy", "spend_money"])
    assert t["task"]["status"] == "awaiting_approval" and len(t["approvals"]) == 2
    assert store.claim_next_task("a")["task"] is None
    with pytest.raises(BridgeError, match="not leased"):
        store.update_task("a", t["task"]["id"], status="in_progress")

    first, second = t["approvals"]
    store.decide_approval(first["id"], True, "keagen")
    assert store.claim_next_task("a")["task"] is None  # one approval still pending
    store.decide_approval(second["id"], True, "keagen")
    assert store.claim_next_task("a")["task"]["id"] == t["task"]["id"]
    with pytest.raises(BridgeError, match="already approved"):
        store.decide_approval(first["id"], False, "keagen")


def test_rejected_approval_cancels_task(store):
    make_project(store)
    t = store.create_task("a", "Demo", "Email customers", sensitive_actions=["send_external_message"])
    res = store.decide_approval(t["approvals"][0]["id"], False, "keagen", reason="not yet")
    assert res["task"]["status"] == "cancelled"
    assert store.claim_next_task("a")["task"] is None


def test_unknown_references_are_rejected(store):
    with pytest.raises(BridgeError, match="Unknown project"):
        store.create_task("a", "nope", "t")
    make_project(store)
    with pytest.raises(BridgeError, match="Unknown sensitive"):
        store.create_task("a", "Demo", "t", sensitive_actions=["launch_missiles"])
    with pytest.raises(BridgeError, match="Unknown task"):
        store.update_task("a", "task_" + "0" * 32, status="done")


def test_writes_stay_inside_the_database_folder(tmp_path):
    """Using the bridge must not touch anything outside its own data folder."""
    neighbour = tmp_path / "other-project"
    neighbour.mkdir()
    (neighbour / "app.py").write_text("print('hi')")
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}

    s = Store(tmp_path / "bridge-data" / "bridge.db")
    s.create_project("a", "Demo")
    s.save_memory("a", "Demo", "note")
    s.create_task("a", "Demo", "t")
    s.claim_next_task("a")
    s.close()

    after = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    changed = {p for p in after if before.get(p) != after[p]}
    assert all(p == tmp_path / "bridge-data" or p.parent == tmp_path / "bridge-data" for p in changed), changed
    assert (neighbour / "app.py").read_text() == "print('hi')"


def test_default_database_lives_in_this_project(monkeypatch):
    monkeypatch.delenv("KTV_BRIDGE_DB", raising=False)
    root = Path(__file__).resolve().parent.parent
    assert default_db_path() == root / "data" / "bridge.db"
