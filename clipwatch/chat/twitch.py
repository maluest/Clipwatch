"""Twitch chat over IRC, read anonymously (no account or token needed)."""
from __future__ import annotations

import random
import socket
import time

from . import ChatReader


class TwitchChat(ChatReader):
    name_for_humans = "twitch"
    host = "irc.chat.twitch.tv"
    port = 6667

    def read(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=30)
        try:
            nick = f"justinfan{random.randint(10000, 99999)}"
            sock.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
            sock.settimeout(1.0)
            buf = b""
            last_data = time.time()
            while not self.stop_event.is_set():
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    if time.time() - last_data > 360:
                        raise ConnectionError("no data from Twitch for 6 minutes")
                    continue
                if not chunk:
                    raise ConnectionError("Twitch closed the connection")
                last_data = time.time()
                buf += chunk
                *lines, buf = buf.split(b"\r\n")
                for raw in lines:
                    line = raw.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        sock.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                        continue
                    if " JOIN #" in line and not self.connected:
                        self.connected = True
                        self.log(f"[twitch] reading chat of #{self.channel}")
                    text = parse_privmsg(line)
                    if text is not None:
                        self.on_message(time.time(), text)
        finally:
            sock.close()


def parse_privmsg(line: str) -> str | None:
    # :user!user@user.tmi.twitch.tv PRIVMSG #channel :message text
    if line.startswith("@"):
        _, _, line = line.partition(" ")
    parts = line.split(" ", 3)
    if len(parts) == 4 and parts[1] == "PRIVMSG":
        return parts[3][1:] if parts[3].startswith(":") else parts[3]
    return None
