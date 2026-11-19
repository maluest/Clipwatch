"""Kick chat via Kick's public Pusher websocket.

Kick has no official public chat API, so this uses what the website itself uses.
If Kick changes it, clipwatch falls back to audio-based detection.
"""
from __future__ import annotations

import json
import time

from . import ChatReader

DEFAULT_PUSHER_KEY = "32cbd69e4b950bf97679"
PUSHER_URL = "wss://ws-us2.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0-rc2&flash=false"


def get_chatroom_id(slug: str) -> int:
    from curl_cffi import requests  # looks like a real browser, which Kick's Cloudflare wants

    r = requests.get(f"https://kick.com/api/v2/channels/{slug}", impersonate="chrome", timeout=20)
    r.raise_for_status()
    return int(r.json()["chatroom"]["id"])


class KickChat(ChatReader):
    name_for_humans = "kick"

    def __init__(self, channel, on_message, log=print, pusher_key: str | None = None):
        super().__init__(channel, on_message, log)
        self.pusher_key = pusher_key or DEFAULT_PUSHER_KEY
        self._room: int | None = None

    def read(self) -> None:
        import websocket

        if self._room is None:
            self._room = get_chatroom_id(self.channel)
        ws = websocket.create_connection(PUSHER_URL.format(key=self.pusher_key), timeout=30)
        try:
            ws.send(json.dumps({"event": "pusher:subscribe",
                                "data": {"auth": "", "channel": f"chatrooms.{self._room}.v2"}}))
            ws.settimeout(1.0)
            last_data = time.time()
            while not self.stop_event.is_set():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    if time.time() - last_data > 120:
                        ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
                        last_data = time.time()
                    continue
                last_data = time.time()
                if not raw:
                    raise ConnectionError("Kick closed the connection")
                ev = json.loads(raw)
                name = ev.get("event", "")
                if name == "pusher_internal:subscription_succeeded":
                    self.connected = True
                    self.log(f"[kick] reading chat of {self.channel}")
                elif name == "pusher:ping":
                    ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                elif name.endswith("ChatMessageEvent"):
                    data = ev.get("data")
                    if isinstance(data, str):
                        data = json.loads(data)
                    self.on_message(time.time(), (data or {}).get("content", ""))
        finally:
            ws.close()
