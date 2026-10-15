import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.fake_odoo import FakeOdoo


class ScriptedLLM:
    """Replays queued assistant messages; records what it was sent."""
    def __init__(self):
        self.queue, self.seen = [], []

    def chat(self, messages, tools):
        self.seen.append(messages)
        return self.queue.pop(0)


def tool_call(name, args, id="c1"):
    import json
    return {"content": "", "tool_calls": [{"id": id, "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture
def ctx(tmp_path):
    settings = Settings(api_keys={"k1": "alice", "k2": "bob"}, confirm_callers={"alice"},
                        audit_log_path=str(tmp_path / "audit.log"), rate_limit_per_minute=1000)
    odoo, llm = FakeOdoo(), ScriptedLLM()
    return TestClient(create_app(settings, odoo, llm)), odoo, llm
