"""Speech-to-text (faster-whisper) and the big bouncy word-by-word captions used in shorts."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_model_cache: dict = {}


@dataclass
class Word:
    start: float
    end: float
    text: str


def transcribe(wav: Path, model_name: str = "small", device: str = "auto", log=print) -> list[Word]:
    from faster_whisper import WhisperModel

    key = (model_name, device)
    if key not in _model_cache:
        log(f"[captions] loading speech model '{model_name}'. The FIRST time this downloads ~0.5 GB, "
            "which can take several minutes. Later clips skip this.")
        if device == "auto":
            try:
                _model_cache[key] = WhisperModel(model_name, device="cuda", compute_type="float16")
            except Exception:  # noqa: BLE001 - no NVIDIA GPU / CUDA: use the CPU
                _model_cache[key] = WhisperModel(model_name, device="cpu", compute_type="int8")
        else:
            _model_cache[key] = WhisperModel(model_name, device=device,
                                             compute_type="float16" if device == "cuda" else "int8")
    model = _model_cache[key]
    segments, _info = model.transcribe(str(wav), word_timestamps=True, vad_filter=True)
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            t = w.word.strip()
            if t:
                words.append(Word(w.start, w.end, t))
    return words


def group_words(words: list[Word], max_words: int = 3, max_gap: float = 0.6) -> list[list[Word]]:
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap or re.search(r"[.!?,]$", cur[-1].text)):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def _ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _clean(text: str) -> str:
    return text.upper().replace("\\", "").replace("{", "(").replace("}", ")")


def build_ass(words: list[Word], width: int = 1080, height: int = 1920, font: str = "Arial",
              margin_v: int = 560, center_y: int | None = None) -> str:
    """ASS subtitle file: 2-3 words at a time, the word being spoken in yellow.
    center_y: put the captions' middle at this height (e.g. on the split-screen seam)."""
    align = 5 if center_y is not None else 2
    place = rf"{{\pos({width // 2},{center_y})}}" if center_y is not None else ""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,{align},60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for group in group_words(words):
        for i, w in enumerate(group):
            start = w.start
            end = group[i + 1].start if i + 1 < len(group) else w.end + 0.15
            if end <= start:
                end = start + 0.08
            parts = []
            for j, g in enumerate(group):
                t = _clean(g.text)
                parts.append(r"{\c&H00FFFF&}" + t + r"{\c&HFFFFFF&}" if j == i else t)
            pop = r"{\fscx108\fscy108\t(0,90,\fscx100\fscy100)}" if i == 0 else ""
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{place}{pop}{' '.join(parts)}")
    return header + "\n".join(lines) + "\n"
