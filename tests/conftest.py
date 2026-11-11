import sys
from datetime import datetime, timedelta, timezone

import pytest
from mcp import Client, StdioServerParameters

from ktv_bridge.storage import Store


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "bridge.db", lease_seconds=300, clock=clock)
    yield s
    s.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "shared" / "bridge.db"


@pytest.fixture
def connect(db_path):
    """Start a real server subprocess over stdio, like Claude Code or any other MCP client would."""
    def _connect():
        return Client(StdioServerParameters(command=sys.executable, args=["-m", "ktv_bridge"],
                                            env={"KTV_BRIDGE_DB": str(db_path)}))
    return _connect


async def call(client, tool, **args):
    r = await client.call_tool(tool, args)
    assert not r.is_error, r.content[0].text
    return r.structured_content
