"""Work out which site a stream link belongs to."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Stream:
    platform: str  # "twitch" | "youtube" | "kick" | "file"
    url: str
    channel: str  # channel name / slug / video id, used for chat and folder names


def parse_stream_url(url: str) -> Stream:
    u = url.strip()
    m = re.match(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]+)/?(?:\?.*)?$", u)
    if m:
        name = m.group(1).lower()
        return Stream("twitch", f"https://www.twitch.tv/{name}", name)

    m = re.match(r"^(?:https?://)?(?:www\.)?kick\.com/([A-Za-z0-9_\-]+)/?(?:\?.*)?$", u)
    if m:
        slug = m.group(1).lower()
        return Stream("kick", f"https://kick.com/{slug}", slug)

    m = re.match(r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/watch\?(?:.*&)?v=([A-Za-z0-9_\-]{11})", u) \
        or re.match(r"^(?:https?://)?youtu\.be/([A-Za-z0-9_\-]{11})", u) \
        or re.match(r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/live/([A-Za-z0-9_\-]{11})", u)
    if m:
        vid = m.group(1)
        return Stream("youtube", f"https://www.youtube.com/watch?v={vid}", vid)

    m = re.match(r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/(@[A-Za-z0-9_.\-]+)(?:/live)?/?$", u)
    if m:
        handle = m.group(1)
        return Stream("youtube", f"https://www.youtube.com/{handle}/live", handle.lstrip("@"))

    raise ValueError(
        f"Don't recognise this link: {url}\n"
        "Use a Twitch, Kick or YouTube live link, e.g. https://www.twitch.tv/name, "
        "https://kick.com/name, https://www.youtube.com/@name/live or a youtube.com/watch?v=... link."
    )
