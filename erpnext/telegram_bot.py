"""Thin Telegram client for the middleware: voice -> text (speech-to-text API), then POST /v1/prompt.

Each Telegram user is mapped to a middleware caller (TELEGRAM_USERS), and each caller acts in ERPNext as its own
ERPNext user. All ERPNext logic, confirmation and security live in the middleware (app/). Start it with:
    venv/bin/uvicorn app.main:app_from_env --factory --port 8100
    venv/bin/python telegram_bot.py
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import time
from dataclasses import dataclass

import requests

from app.config import load_dotenv

LOG = logging.getLogger("telegram-bot")


@dataclass
class BotConfig:
    telegram_token: str
    stt_key: str
    users: dict[str, str]          # telegram user id -> middleware caller
    keys: dict[str, str]           # middleware caller -> API key
    stt_base_url: str = "https://api.groq.com/openai/v1"
    stt_model: str = "whisper-large-v3"
    stt_language: str = "en"   # English by default; auto-detect can mistake it for another language

    @property
    def allowed_user_ids(self) -> set[str]:
        return set(self.users)

    @classmethod
    def from_env(cls) -> "BotConfig":
        def need(name):
            if not os.environ.get(name, "").strip():
                raise RuntimeError(f"Missing required environment variable: {name}")
            return os.environ[name].strip()
        return cls(need("TELEGRAM_BOT_TOKEN"), os.environ.get("STT_API_KEY", "").strip(),
                   _pairs(os.environ.get("TELEGRAM_USERS", "")), _pairs(need("MIDDLEWARE_KEYS")),
                   os.environ.get("STT_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
                   os.environ.get("STT_MODEL", "whisper-large-v3"), os.environ.get("STT_LANGUAGE", "en").strip())


def _pairs(raw: str) -> dict[str, str]:
    return {k.strip(): v.strip() for k, v in (p.split(":", 1) for p in raw.split(",") if ":" in p) if k.strip() and v.strip()}


class TelegramClient:
    def __init__(self, config: BotConfig):
        self.token = config.telegram_token
        self.base = f"https://api.telegram.org/bot{self.token}"

    def call(self, method: str, **params):
        r = requests.post(f"{self.base}/{method}", json=params, timeout=45)
        body = r.json() if r.content else {}
        if not r.ok or not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed ({r.status_code}): {r.text[:300]}")
        return body["result"]

    def delete_webhook(self):
        return self.call("deleteWebhook", drop_pending_updates=False)

    def updates(self, offset: int | None):
        params = {"timeout": 25, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", **params)

    def send(self, chat_id, text: str):
        return self.call("sendMessage", chat_id=chat_id, text=text)

    def download(self, file_id: str) -> tuple[str, bytes]:
        path = self.call("getFile", file_id=file_id)["file_path"]
        r = requests.get(f"https://api.telegram.org/file/bot{self.token}/{path}", timeout=60)
        r.raise_for_status()
        return path, r.content


class Transcriber:
    def __init__(self, config: BotConfig):
        self.config = config

    def transcribe(self, filename: str, content: bytes, mime: str = "audio/ogg", vocabulary: str = "") -> str:
        data = {"model": self.config.stt_model, "response_format": "json", "temperature": "0",
                "prompt": ("Sales CRM vocabulary: leads, customers, quotations, calls, meetings, BOQ, opportunities. " + vocabulary)[:700]}
        if self.config.stt_language:  # auto-detect can mistake one language for a close one
            data["language"] = self.config.stt_language
        if not self.config.stt_key:
            raise RuntimeError("Voice notes need STT_API_KEY")
        r = requests.post(f"{self.config.stt_base_url}/audio/transcriptions", timeout=90,
                          headers={"Authorization": f"Bearer {self.config.stt_key}"},
                          files={"file": (filename, content, mime)}, data=data)
        if not r.ok:
            raise RuntimeError(f"Transcription failed ({r.status_code}): {r.text[:300]}")
        return r.json().get("text", "").strip()
MAX_VOICE_BYTES = 10 * 1024 * 1024  # ~ many minutes of opus; speech APIs accept up to 25 MB
MAX_VOICE_SECONDS = 180


GROQ_EXT = {"flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "opus", "wav", "webm"}
MIME = {"ogg": "audio/ogg", "opus": "audio/ogg", "mp3": "audio/mpeg", "mpeg": "audio/mpeg", "mpga": "audio/mpeg",
        "m4a": "audio/mp4", "mp4": "audio/mp4", "wav": "audio/wav", "flac": "audio/flac", "webm": "audio/webm"}


def upload_args(path: str, content: bytes) -> tuple[str, bytes, str]:
    """Telegram voice notes are Opus-in-Ogg named *.oga, an extension some speech APIs reject; present them as .ogg."""
    stem, _, ext = path.rsplit("/", 1)[-1].rpartition(".")
    ext = "ogg" if ext.lower() in ("oga", "ogv") or ext.lower() not in GROQ_EXT else ext.lower()
    return f"{stem or 'voice'}.{ext}", content, MIME[ext]


def plain(text: str) -> str:
    """Telegram is sent as plain text: drop markdown emphasis/table syntax the model sometimes adds."""
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"^\s*\|?[-:| ]{3,}\|?\s*$", "", text, flags=re.M)  # table separator rows
    text = re.sub(r"^\s*\|(.*)\|\s*$", lambda m: " - ".join(c.strip() for c in m.group(1).split("|") if c.strip()), text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class Bot:
    def __init__(self, config: BotConfig, telegram: TelegramClient, stt: Transcriber, url: str, api_key: str = ""):
        self.config, self.telegram, self.stt = config, telegram, stt
        self.url = url.rstrip("/")
        self.api_key = api_key  # fallback key for tests; real keys come from config.keys per caller
        self._vocab, self._vocab_at = "", 0.0

    def vocabulary(self) -> str:
        """Real customer/product names help Whisper spell them right; refreshed every 10 minutes, optional."""
        key = next(iter(getattr(self.config, "keys", {}).values()), self.api_key)
        if key and time.time() - self._vocab_at > 600:
            self._vocab_at = time.time()
            try:
                r = requests.get(f"{self.url}/v1/vocabulary", headers={"X-API-Key": key}, timeout=10)
                if r.ok:
                    d = r.json()
                    self._vocab = "Names: " + ", ".join([*d["customers"], *d["products"]]) + "."
            except (requests.RequestException, ValueError, KeyError):
                pass
        return self._vocab

    def authorized(self, user_id: str) -> bool:
        return user_id in self.config.allowed_user_ids

    def text_of(self, message: dict) -> str:
        if message.get("text"):
            return message["text"].strip()
        media = message.get("voice") or message.get("audio")
        if media:
            if media.get("duration", 0) > MAX_VOICE_SECONDS or media.get("file_size", 0) > MAX_VOICE_BYTES:
                raise ValueError("voice note too long")
            path, content = self.telegram.download(media["file_id"])
            return self.stt.transcribe(*upload_args(path, content), vocabulary=self.vocabulary())
        return ""

    def ask(self, user_id: str, text: str) -> str:
        caller = getattr(self.config, "users", {}).get(user_id)
        key = self.config.keys.get(caller, self.api_key) if caller else self.api_key
        r = requests.post(f"{self.url}/v1/prompt", json={"message": text, "session_id": user_id},
                          headers={"X-API-Key": key}, timeout=120)
        if r.status_code == 429:
            return "You are sending messages too quickly. Please wait a minute."
        if not r.ok:
            LOG.error("Middleware %s: %s", r.status_code, r.text[:300])
            return "The assistant is unavailable right now. Please try again shortly."
        return r.json()["reply"]

    def handle(self, message: dict) -> None:
        user_id, chat_id = str(message.get("from", {}).get("id", "")), message.get("chat", {}).get("id")
        if not chat_id:
            return
        if message.get("text", "").strip() == "/id":
            self.telegram.send(chat_id, f"Your Telegram user ID is {user_id}.")
            return
        if not self.authorized(user_id):
            self.telegram.send(chat_id, "This Telegram account is not authorised. Send /id and ask the administrator.")
            return
        try:
            text = self.text_of(message)
            if not text:
                self.telegram.send(chat_id, "I could not understand that. Please send text or a clear voice note.")
                return
            if message.get("voice") or message.get("audio"):
                self.telegram.send(chat_id, f"Heard: {text}")
            self.telegram.send(chat_id, plain(self.ask(user_id, text)))
        except ValueError:
            self.telegram.send(chat_id, f"That voice note is too long. Please keep it under {MAX_VOICE_SECONDS // 60} minutes.")
        except Exception:
            LOG.exception("Failed to handle message")
            self.telegram.send(chat_id, "Something went wrong. Nothing was changed in ERPNext.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    config = BotConfig.from_env()
    bot = Bot(config, TelegramClient(config), Transcriber(config), os.environ.get("MIDDLEWARE_URL", "http://127.0.0.1:8100"))
    bot.telegram.delete_webhook()
    LOG.info("Telegram bot started. Allowed users: %s", config.users)
    offset = None
    while True:
        try:
            for update in bot.telegram.updates(offset):
                offset = update["update_id"] + 1
                if update.get("message"):
                    bot.handle(update["message"])
        except KeyboardInterrupt:
            break
        except Exception as exc:
            if "(409)" in str(exc):  # Telegram allows one getUpdates poller per token
                LOG.warning("Another process is polling this bot token (HTTP 409). Stop the other bot "
                            "(for example the Odoo project's bot, or a second copy of this one) or use a separate token.")
                time.sleep(10)
                continue
            LOG.exception("Polling error")
            time.sleep(3)


if __name__ == "__main__":
    main()
