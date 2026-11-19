"""Decide when chat (or the streamer's audio) has 'gone crazy'.

The idea: keep a rolling baseline of how busy chat normally is, and fire when the
last few seconds are several times busier than that. Hype words (LUL, KEKW, "no way"...)
count extra. A cooldown stops one moment producing five clips.
"""
from __future__ import annotations

import re
import statistics
import threading
from collections import deque
from dataclasses import dataclass, field

HYPE = re.compile(
    r"(?i)\b(lul|lmao+|lmfao|kekw|omegalul|pog+|poggers|pogchamp|w+|holy|no ?way|clip+ ?(it|that)?|"
    r"what+|wtf|omg|bro+|insane|crazy|💀|😂|🤣)\b|💀|😂|🤣|🔥"
)


@dataclass
class Spike:
    time: float  # wall-clock seconds when the spike was detected
    score: float  # how many times busier than normal
    reason: str  # "chat" or "audio"
    messages: list[str] = field(default_factory=list)  # chat lines around the moment


class ChatSpikeDetector:
    def __init__(
        self,
        window: float = 8.0,
        baseline_span: float = 300.0,
        multiplier: float = 3.0,
        min_messages: int = 8,
        cooldown: float = 60.0,
        warmup: float = 45.0,
    ):
        self.window = window
        self.baseline_span = baseline_span
        self.multiplier = multiplier
        self.min_messages = min_messages
        self.cooldown = cooldown
        self.warmup = warmup
        self._msgs: deque[tuple[float, str]] = deque()
        self._rates: deque[tuple[float, float]] = deque()  # (time, weighted msgs per window)
        self._start: float | None = None
        self._last_fire = -1e18
        self._lock = threading.Lock()

    def add_message(self, t: float, text: str) -> None:
        with self._lock:
            if self._start is None:
                self._start = t
            self._msgs.append((t, text))

    def _weighted_count(self, now: float) -> tuple[float, int]:
        lo = now - self.window
        n = 0
        w = 0.0
        for t, text in self._msgs:
            if lo < t <= now:
                n += 1
                w += 1.5 if HYPE.search(text) else 1.0
        return w, n

    def tick(self, now: float) -> Spike | None:
        """Call about once a second. Returns a Spike when one is detected."""
        with self._lock:
            if self._start is None:
                self._start = now
            # forget old messages
            keep_from = now - max(self.window, 30.0)
            while self._msgs and self._msgs[0][0] < keep_from:
                self._msgs.popleft()

            weighted, count = self._weighted_count(now)

            # baseline = median busy-ness over the last few minutes, ignoring the most recent 20s
            past = [r for (t, r) in self._rates if now - self.baseline_span <= t <= now - 20]
            baseline = statistics.median(past) if past else 0.0
            # A floor so a near-silent chat doesn't fire on 3 messages
            baseline = max(baseline, self.min_messages / self.multiplier)

            self._rates.append((now, weighted))
            while self._rates and self._rates[0][0] < now - self.baseline_span:
                self._rates.popleft()

            if now - self._start < self.warmup:
                return None
            if now - self._last_fire < self.cooldown:
                return None
            if count >= self.min_messages and weighted >= baseline * self.multiplier:
                self._last_fire = now
                lines = [text for t, text in self._msgs if t > now - 20]
                return Spike(now, round(weighted / baseline, 1), "chat", lines[-40:])
            return None


class AudioSpikeDetector:
    """Fallback when chat can't be read: fire when the stream gets much louder than usual
    (streamer screaming, explosions...). Fed one loudness value per recorded segment."""

    def __init__(self, jump_db: float = 9.0, history: int = 60, cooldown: float = 60.0, warmup: int = 6):
        self.jump_db = jump_db
        self.cooldown = cooldown
        self.warmup = warmup
        self._levels: deque[float] = deque(maxlen=history)
        self._last_fire = -1e18

    def add_level(self, t: float, mean_db: float) -> Spike | None:
        prev = list(self._levels)
        self._levels.append(mean_db)
        if len(prev) < self.warmup or mean_db < -60:
            return None
        base = statistics.median(prev)
        if mean_db - base >= self.jump_db and t - self._last_fire >= self.cooldown:
            self._last_fire = t
            return Spike(t, round(mean_db - base, 1), "audio")
        return None
