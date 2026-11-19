"""YouTube live chat via the chat-downloader library."""
from __future__ import annotations

import time

from . import ChatReader


class YouTubeChat(ChatReader):
    name_for_humans = "youtube"

    def read(self) -> None:
        from chat_downloader import ChatDownloader

        chat = ChatDownloader().get_chat(self.channel, message_groups=["messages", "superchat"])
        self.connected = True
        self.log("[youtube] reading live chat")
        for msg in chat:
            if self.stop_event.is_set():
                return
            text = msg.get("message") or ""
            self.on_message(time.time(), text)
        raise ConnectionError("YouTube chat ended")
