import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.stub_erp import StubERPNext, by_caller


class ScriptedLLM:
    """Replays queued assistant messages; records what it was sent."""
    def __init__(self):
        self.queue, self.seen = [], []

    def chat(self, messages, tools):
        self.seen.append(messages)
        return self.queue.pop(0)


def tool_call(name, args, id="c1"):
    return {"content": "", "tool_calls": [{"id": id, "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture
def ctx(tmp_path):
    """TestClient plus alice's and bob's ERPNext stubs and the scripted LLM. 'alice' is not a confirm caller."""
    settings = Settings(api_keys={"k1": "alice", "k2": "bob", "k3": "manager"}, confirm_callers={"manager"}, item_creators={"manager"},
                        audit_log_path=str(tmp_path / "audit.log"), rate_limit_per_minute=1000)
    alice, bob = StubERPNext("alice@example.com"), StubERPNext("bob@example.com")
    manager = StubERPNext("manager@example.com")
    llm = ScriptedLLM()
    client = TestClient(create_app(settings, by_caller(alice=alice, bob=bob, manager=manager), llm))
    client.stubs = {"alice": alice, "bob": bob, "manager": manager}
    return client, alice, llm
