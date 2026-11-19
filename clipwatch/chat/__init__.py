"""Live chat readers. Each runs in a background thread and calls on_message(time, text)."""
from __future__ import annotations

import json
import threading
import time
from typing import Callable

OnMessage = Callable[[float, str], None]


class ChatReader(threading.Thread):
    name_for_humans = "chat"

    def __init__(self, channel: str, on_message: OnMessage, log: Callable[[str], None] = print):
        super().__init__(daemon=True)
        self.channel = channel
        self.on_message = on_message
        self.log = log
        self.stop_event = threading.Event()
        self.connected = False
        self.failed: str | None = None

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:  # retry loop
        backoff = 2
        while not self.stop_event.is_set():
            try:
                self.read()
                backoff = 2
            except Exception as e:  # noqa: BLE001 - keep going, chat drops are normal
                self.connected = False
                self.log(f"[{self.name_for_humans}] chat connection problem: {e} (retrying in {backoff}s)")
            if self.stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, 60)

    def read(self) -> None:
        raise NotImplementedError


class ReplayChat(ChatReader):
    """Replays chat from a .jsonl file of {"t": seconds_from_start, "text": "..."} lines.
    Used for testing without a real stream."""

    name_for_humans = "replay"

    def __init__(self, path: str, on_message: OnMessage, log=print):
        super().__init__(path, on_message, log)

    def run(self) -> None:
        start = time.time()
        self.connected = True
        with open(self.channel, encoding="utf-8") as f:
            for line in f:
                if self.stop_event.is_set():
                    return
                msg = json.loads(line)
                delay = start + msg["t"] - time.time()
                if delay > 0 and self.stop_event.wait(delay):
                    return
                self.on_message(time.time(), msg["text"])


def make_chat_reader(platform: str, channel: str, url: str, on_message: OnMessage, log=print, kick_key: str | None = None):
    if platform == "twitch":
        from .twitch import TwitchChat
        return TwitchChat(channel, on_message, log)
    if platform == "youtube":
        from .youtube import YouTubeChat
        return YouTubeChat(url, on_message, log)
    if platform == "kick":
        from .kick import KickChat
        return KickChat(channel, on_message, log, pusher_key=kick_key)
    return None
