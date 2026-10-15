from telegram_bot import Bot


class FakeTelegram:
    def __init__(self): self.sent = []
    def send(self, chat_id, text): self.sent.append(text)


class Cfg:
    allow_all = False
    allowed_user_ids = {"42"}


def msg(uid, text):
    return {"from": {"id": uid}, "chat": {"id": 1}, "text": text}


def test_unauthorised_user_never_reaches_middleware():
    tg, bot = FakeTelegram(), None
    bot = Bot(Cfg(), tg, None, "http://x", "k")
    bot.ask = lambda *a: (_ for _ in ()).throw(AssertionError("must not call middleware"))
    bot.handle(msg(7, "create order"))
    assert "not authorised" in tg.sent[0]
    bot.handle(msg(7, "/id"))
    assert "7" in tg.sent[1]


def test_authorised_text_is_forwarded():
    tg = FakeTelegram()
    bot = Bot(Cfg(), tg, None, "http://x", "k")
    bot.ask = lambda uid, text: f"echo {uid}:{text}"
    bot.handle(msg(42, "hello"))
    assert tg.sent == ["echo 42:hello"]


class FakeTelegramMedia(FakeTelegram):
    def __init__(self, content=b"audio-bytes", path="voice/file_1.oga"):
        super().__init__()
        self.content, self.path, self.downloads = content, path, 0

    def download(self, file_id):
        self.downloads += 1
        return self.path, self.content


class FakeGroq:
    def __init__(self, text): self.text, self.calls = text, []

    def transcribe(self, name, content, mime="audio/ogg", vocabulary=""):
        self.calls.append((name, content, mime))
        self.vocabulary = vocabulary
        return self.text


def voice(uid, **kw):
    return {"from": {"id": uid}, "chat": {"id": 1}, "voice": {"file_id": "f", "duration": 5, **kw}}


def test_voice_is_transcribed_then_forwarded():
    tg, groq = FakeTelegramMedia(), FakeGroq("create a quotation for xyz traders")
    bot = Bot(Cfg(), tg, groq, "http://x", "k")
    bot.ask = lambda uid, text: f"asked:{text}"
    bot.vocabulary = lambda: "Names: Nizwa Builders."
    bot.handle(voice(42))
    assert groq.vocabulary == "Names: Nizwa Builders."  # real names are passed to Whisper as a hint
    assert groq.calls == [("file_1.ogg", b"audio-bytes", "audio/ogg")]  # .oga is rejected by Groq
    assert tg.sent == ["Heard: create a quotation for xyz traders", "asked:create a quotation for xyz traders"]


def test_voice_from_unauthorised_user_is_never_downloaded():
    tg, groq = FakeTelegramMedia(), FakeGroq("x")
    bot = Bot(Cfg(), tg, groq, "http://x", "k")
    bot.handle(voice(7))
    assert tg.downloads == 0 and groq.calls == []


def test_overlong_voice_is_refused_before_download():
    tg, groq = FakeTelegramMedia(), FakeGroq("x")
    bot = Bot(Cfg(), tg, groq, "http://x", "k")
    bot.handle(voice(42, duration=900))
    assert tg.downloads == 0 and "too long" in tg.sent[0]


def test_empty_transcript_gets_a_helpful_reply():
    tg = FakeTelegramMedia()
    bot = Bot(Cfg(), tg, FakeGroq(""), "http://x", "k")
    bot.handle(voice(42))
    assert "could not understand" in tg.sent[0]


def test_markdown_is_flattened_for_telegram():
    from telegram_bot import plain
    out = plain("**Orders**\n| ID | Total |\n|----|-------|\n| S1 | 10 |")
    assert "**" not in out and "|" not in out and "S1 - 10" in out


def test_upload_args_normalises_extensions():
    from telegram_bot import upload_args
    assert upload_args("voice/file_5.oga", b"")[::2] == ("file_5.ogg", "audio/ogg")
    assert upload_args("music/a.mp3", b"")[::2] == ("a.mp3", "audio/mpeg")
    assert upload_args("voice/noext", b"")[0].endswith(".ogg")
