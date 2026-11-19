"""YouTube live chat, read with yt-dlp.

yt-dlp can save a live stream's chat to a file as it happens (one JSON line per message).
We start that in the background and read new lines as they arrive. yt-dlp is kept up to
date with YouTube's changes, unlike most standalone chat libraries.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import ChatReader


def message_text(line: str) -> str | None:
    """Pull the text out of one line of yt-dlp's live_chat.json."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    actions = (data.get("replayChatItemAction") or {}).get("actions") or [data]
    for action in actions:
        item = (action.get("addChatItemAction") or {}).get("item") or {}
        r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not r:
            continue
        parts = []
        for run in (r.get("message") or {}).get("runs", []):
            if "text" in run:
                parts.append(run["text"])
            elif "emoji" in run:
                e = run["emoji"]
                parts.append((e.get("shortcuts") or [e.get("emojiId", "")])[0])
        return "".join(parts)
    return None


class YouTubeChat(ChatReader):
    name_for_humans = "youtube"

    def read(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="clipwatch_ytchat_"))
        cmd = [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--skip-download",
               "--write-subs", "--sub-langs", "live_chat", "--no-part",
               "-o", str(tmp / "chat.%(ext)s"), self.channel]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, errors="replace")
        f = None
        buf = ""
        try:
            while not self.stop_event.is_set():
                if f is None:
                    found = sorted(tmp.glob("chat*.json*"))
                    if found:
                        f = open(found[0], encoding="utf-8", errors="replace")
                        self.connected = True
                        self.log("[youtube] reading live chat")
                if f is not None:
                    chunk = f.read()
                    if chunk:
                        buf += chunk
                        *lines, buf = buf.split("\n")
                        for line in lines:
                            text = message_text(line)
                            if text is not None:
                                self.on_message(time.time(), text)
                        continue
                if proc.poll() is not None:
                    err = (proc.stderr.read() or "").strip().splitlines()
                    raise ConnectionError(err[-1] if err else "YouTube chat ended")
                self.stop_event.wait(0.5)
        finally:
            if proc.poll() is None:
                proc.terminate()
            if f is not None:
                f.close()
