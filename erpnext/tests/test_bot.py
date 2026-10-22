import telegram_bot
from telegram_bot import Bot, BotConfig, Transcriber, plain, upload_args


class StubTelegram:
    def __init__(self, content=b"audio-bytes", path="voice/file_1.oga"):
        self.sent, self.content, self.path, self.downloads = [], content, path, 0

    def send(self, chat_id, text):
        self.sent.append(text)

    def download(self, file_id):
        self.downloads += 1
        return self.path, self.content


class StubSTT:
    def __init__(self, text):
        self.text, self.calls, self.vocabulary = text, [], ""

    def transcribe(self, name, content, mime="audio/ogg", vocabulary=""):
        self.calls.append((name, content, mime))
        self.vocabulary = vocabulary
        return self.text


def cfg():
    return BotConfig("tok", "stt", {"42": "alice", "43": "bob"}, {"alice": "key-a", "bob": "key-b"})


def msg(uid, text):
    return {"from": {"id": uid}, "chat": {"id": 1}, "text": text}


def voice(uid, **kw):
    return {"from": {"id": uid}, "chat": {"id": 1}, "voice": {"file_id": "f", "duration": 5, **kw}}


def test_unauthorised_user_never_reaches_middleware():
    tg = StubTelegram()
    bot = Bot(cfg(), tg, None, "http://x")
    bot.ask = lambda *a: (_ for _ in ()).throw(AssertionError("must not call middleware"))
    bot.handle(msg(7, "log a meeting"))
    assert "not authorised" in tg.sent[0]
    bot.handle(msg(7, "/id"))
    assert "7" in tg.sent[1]


def test_authorised_text_is_forwarded():
    tg = StubTelegram()
    bot = Bot(cfg(), tg, None, "http://x")
    bot.ask = lambda uid, text: f"echo {uid}:{text}"
    bot.handle(msg(42, "hello"))
    assert tg.sent == ["echo 42:hello"]


def test_each_telegram_user_calls_the_middleware_as_their_own_caller(monkeypatch):
    seen = []

    class R:
        ok, status_code = True, 200
        def json(self): return {"reply": "ok"}
    monkeypatch.setattr(telegram_bot.requests, "post", lambda url, json=None, headers=None, timeout=None: seen.append((url, json, headers)) or R())
    bot = Bot(cfg(), StubTelegram(), None, "http://mw:8100/")
    bot.ask("42", "hi")
    bot.ask("43", "yo")
    assert seen[0][0] == "http://mw:8100/v1/prompt" and seen[0][2] == {"X-API-Key": "key-a"} and seen[0][1]["session_id"] == "42"
    assert seen[1][2] == {"X-API-Key": "key-b"}


def test_middleware_errors_become_friendly_replies(monkeypatch):
    class R:
        def __init__(self, code): self.ok, self.status_code, self.text = code < 400, code, "boom"
    for code, expect in ((429, "too quickly"), (503, "unavailable")):
        monkeypatch.setattr(telegram_bot.requests, "post", lambda *a, code=code, **k: R(code))
        assert expect in Bot(cfg(), StubTelegram(), None, "http://x").ask("42", "hi")


def test_voice_is_transcribed_then_forwarded():
    tg, stt = StubTelegram(), StubSTT("met xyz interiors today")
    bot = Bot(cfg(), tg, stt, "http://x")
    bot.ask = lambda uid, text: f"asked:{text}"
    bot.vocabulary = lambda: "Names: Nizwa Builders."
    bot.handle(voice(42))
    assert stt.vocabulary == "Names: Nizwa Builders."  # real names are passed to the speech model as a hint
    assert stt.calls == [("file_1.ogg", b"audio-bytes", "audio/ogg")]  # .oga is renamed .ogg
    assert tg.sent == ["Heard: met xyz interiors today", "asked:met xyz interiors today"]


def test_voice_from_unauthorised_user_is_never_downloaded():
    tg, stt = StubTelegram(), StubSTT("x")
    Bot(cfg(), tg, stt, "http://x").handle(voice(7))
    assert tg.downloads == 0 and stt.calls == []


def test_overlong_voice_is_refused_before_download():
    tg = StubTelegram()
    Bot(cfg(), tg, StubSTT("x"), "http://x").handle(voice(42, duration=900))
    assert tg.downloads == 0 and "too long" in tg.sent[0]


def test_empty_transcript_gets_a_helpful_reply():
    tg = StubTelegram()
    Bot(cfg(), tg, StubSTT(""), "http://x").handle(voice(42))
    assert "could not understand" in tg.sent[0]


def test_transcriber_uses_configured_endpoint_and_requires_key(monkeypatch):
    captured = {}

    class R:
        ok = True
        def json(self): return {"text": " hello "}
    monkeypatch.setattr(telegram_bot.requests, "post", lambda url, **kw: captured.update(url=url, **kw) or R())
    c = BotConfig("t", "sk", {}, {}, stt_base_url="https://api.openai.com/v1", stt_model="whisper-1", stt_language="ar")
    assert Transcriber(c).transcribe("a.ogg", b"x") == "hello"
    assert captured["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert captured["data"]["model"] == "whisper-1" and captured["data"]["language"] == "ar"
    import pytest
    with pytest.raises(RuntimeError):
        Transcriber(BotConfig("t", "", {}, {})).transcribe("a.ogg", b"x")


def test_markdown_is_flattened_for_telegram():
    out = plain("**Orders**\n| ID | Total |\n|----|-------|\n| S1 | 10 |")
    assert "**" not in out and "|" not in out and "S1 - 10" in out


def test_upload_args_normalises_extensions():
    assert upload_args("voice/file_5.oga", b"")[::2] == ("file_5.ogg", "audio/ogg")
    assert upload_args("music/a.mp3", b"")[::2] == ("a.mp3", "audio/mpeg")
    assert upload_args("voice/noext", b"")[0].endswith(".ogg")


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_USERS", "42:alice, 43:bob")
    monkeypatch.setenv("MIDDLEWARE_KEYS", "alice:ka,bob:kb")
    c = BotConfig.from_env()
    assert c.users == {"42": "alice", "43": "bob"} and c.keys == {"alice": "ka", "bob": "kb"} and c.allowed_user_ids == {"42", "43"}


def test_voice_language_defaults_to_english(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_USERS", "42:alice")
    monkeypatch.setenv("MIDDLEWARE_KEYS", "alice:ka")
    monkeypatch.delenv("STT_LANGUAGE", raising=False)
    assert BotConfig.from_env().stt_language == "en"
    assert BotConfig("t", "k", {}, {}).stt_language == "en"
    monkeypatch.setenv("STT_LANGUAGE", "ar")
    assert BotConfig.from_env().stt_language == "ar"


def test_help_start_and_plain_questions_get_the_fixed_help_without_the_middleware():
    tg = StubTelegram()
    bot = Bot(cfg(), tg, None, "http://x")
    bot.ask = lambda *a, **k: (_ for _ in ()).throw(AssertionError("help must not call the middleware"))
    for text in ("/help", "/start", "/help@my_bot", "help", "What can you do?"):
        bot.handle(msg(42, text))
    assert tg.sent == [telegram_bot.HELP_TEXT] * 5
    assert "/mcp" in telegram_bot.HELP_TEXT and "SALES" in telegram_bot.HELP_TEXT


def test_help_is_not_shown_to_unauthorised_users():
    tg = StubTelegram()
    Bot(cfg(), tg, None, "http://x").handle(msg(7, "/help"))
    assert "not authorised" in tg.sent[0]


def test_mcp_command_forces_mcp_and_needs_a_request():
    tg, seen = StubTelegram(), []
    bot = Bot(cfg(), tg, None, "http://x")
    bot.ask = lambda uid, text, force_mcp=False: seen.append((uid, text, force_mcp)) or "ok"
    bot.handle(msg(42, "/mcp list all warehouses"))
    bot.handle(msg(42, "/mcp@my_bot  count projects "))
    bot.handle(msg(42, "/mcp"))
    bot.handle(msg(42, "plain message"))
    assert seen == [("42", "list all warehouses", True), ("42", "count projects", True), ("42", "plain message", False)]
    assert tg.sent[2].startswith("Usage: /mcp")


def test_force_mcp_flag_is_sent_only_for_mcp(monkeypatch):
    seen = []

    class R:
        ok, status_code = True, 200
        def json(self): return {"reply": "ok"}
    monkeypatch.setattr(telegram_bot.requests, "post", lambda url, json=None, headers=None, timeout=None: seen.append(json) or R())
    bot = Bot(cfg(), StubTelegram(), None, "http://mw")
    bot.ask("42", "hi")
    bot.ask("42", "hi", force_mcp=True)
    assert "force_mcp" not in seen[0] and seen[1]["force_mcp"] is True
