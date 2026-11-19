"""Speech-to-text (faster-whisper) and the big word-by-word captions used in shorts.

Rules for the captions:
* exactly ONE caption on screen at a time: each one ends the moment the next begins;
* each caption is short enough to stay on one line (max 3 words / 15 characters);
* no trailing commas or full stops ("BRO," -> "BRO"); question and exclamation marks stay;
* with a hook, the first 3 seconds show only the hook and captions start after it.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_model_cache: dict = {}

GPU_MODEL = "large-v3-turbo"  # most accurate words and timings, fast on an NVIDIA card
CPU_MODEL = "small"  # what's practical without a GPU

MAX_WORDS = 3
MAX_CHARS = 15
MAX_GAP = 0.6  # a pause longer than this starts a new caption
HOOK_SECONDS = 3.0


@dataclass
class Word:
    start: float
    end: float
    text: str


# ---------------------------------------------------------------- speech-to-text --
def _add_cuda_dlls() -> None:
    """On Windows, faster-whisper finds NVIDIA's CUDA libraries (installed by pip as
    nvidia-cublas-cu12 / nvidia-cudnn-cu12) only if their folders are added like this."""
    if sys.platform != "win32":
        return
    try:
        import nvidia  # namespace package from the nvidia-* wheels
    except ImportError:
        return
    for base in getattr(nvidia, "__path__", []):
        for sub in ("cublas", "cudnn", "cuda_runtime"):
            d = Path(base) / sub / "bin"
            if d.is_dir():
                try:
                    os.add_dll_directory(str(d))
                except OSError:
                    pass
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def _load(model_name: str, device: str, log):
    from faster_whisper import WhisperModel

    key = (model_name, device)
    if key not in _model_cache:
        size = {"large-v3-turbo": "~1.6 GB", "large-v3": "~3 GB", "medium": "~1.5 GB", "small": "~0.5 GB"}.get(model_name, "")
        log(f"[captions] loading speech model '{model_name}' on {'the NVIDIA GPU' if device == 'cuda' else 'the CPU'}. "
            f"The FIRST time this downloads it ({size}), which can take several minutes.")
        _model_cache[key] = WhisperModel(model_name, device=device,
                                         compute_type="float16" if device == "cuda" else "int8")
    return _model_cache[key]


def load_wav(wav: Path):
    """The 16 kHz mono 16-bit WAV clipwatch writes, as float32 samples. Read directly
    (not via faster-whisper's decoder, which breaks with newer PyAV releases)."""
    import wave

    import numpy as np
    with wave.open(str(wav), "rb") as f:
        if f.getnchannels() != 1 or f.getsampwidth() != 2 or f.getframerate() != 16000:
            raise ValueError("expected 16 kHz mono 16-bit audio")
        data = f.readframes(f.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def _run(model, wav: Path, hint: str = "") -> list[Word]:
    segments, _info = model.transcribe(load_wav(wav), word_timestamps=True, vad_filter=True,
                                       condition_on_previous_text=False,
                                       initial_prompt=hint or None)
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            t = w.word.strip()
            if t:
                words.append(Word(float(w.start), float(w.end), t))
    return words


def transcribe(wav: Path, model_name: str = "auto", device: str = "auto", log=print, hint: str = "") -> list[Word]:
    """model_name 'auto' = large-v3-turbo on an NVIDIA GPU, small on the CPU.
    hint: names and words likely to be said (streamer, game), so they're spelled right."""
    if device in ("auto", "cuda"):
        _add_cuda_dlls()
        try:
            return _run(_load(GPU_MODEL if model_name == "auto" else model_name, "cuda", log), wav, hint)
        except Exception as e:  # noqa: BLE001 - no usable GPU: fall back to the CPU
            if device == "cuda":
                raise
            if not _model_cache.get(("_warned",)):
                _model_cache[("_warned",)] = True
                log(f"[captions] NVIDIA GPU not usable ({str(e).splitlines()[0][:120]}); using the CPU instead")
    return _run(_load(CPU_MODEL if model_name == "auto" else model_name, "cpu", log), wav, hint)


# ---------------------------------------------------------------- captions ------------
def clean_word(text: str) -> str:
    """Upper-case, drop trailing commas/full stops/colons, keep ? and !, remove ASS control characters."""
    t = text.strip().upper().replace("\\", "").replace("{", "(").replace("}", ")")
    t = re.sub(r"[,.;:]+$", "", t)
    if t.endswith("...") or t.endswith("…"):
        t = t.rstrip(".…")
    return t


def tidy_timings(words: list[Word]) -> list[Word]:
    """Sort, and make sure no word starts before the previous one has started, and each lasts >= 0.05s."""
    out: list[Word] = []
    for w in sorted(words, key=lambda w: w.start):
        start = max(w.start, out[-1].start + 0.05) if out else w.start
        end = max(w.end, start + 0.05)
        out.append(Word(start, end, w.text))
    return out


def group_words(words: list[Word], max_words: int = MAX_WORDS, max_chars: int = MAX_CHARS,
                max_gap: float = MAX_GAP) -> list[list[Word]]:
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            joined = len(" ".join(clean_word(x.text) for x in cur + [w]))
            if (len(cur) >= max_words or joined > max_chars or w.start - cur[-1].end > max_gap
                    or re.search(r"[.!?,]$", cur[-1].text)):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def _ts(t: float) -> str:
    """ASS time h:mm:ss.cc. Rounded to whole centiseconds first, so 59.996 becomes
    0:01:00.00 (not the invalid 0:00:60.00)."""
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    sec, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"


def caption_events(words: list[Word], skip_until: float = 0.0, linger: float = 0.4) -> list[tuple[float, float, str]]:
    """(start, end, ASS text) per highlighted word. Never two on screen at once."""
    words = [w for w in tidy_timings(words) if w.start >= skip_until and clean_word(w.text)]
    raw = []
    for group in group_words(words):
        for i, w in enumerate(group):
            parts = []
            for j, g in enumerate(group):
                t = clean_word(g.text)
                parts.append(r"{\c&H00FFFF&}" + t + r"{\c&HFFFFFF&}" if j == i else t)
            pop = r"{\fscx108\fscy108\t(0,90,\fscx100\fscy100)}" if i == 0 else ""
            # a word stays up until the next word starts (inside the group), or a little after it ends
            end = group[i + 1].start if i + 1 < len(group) else w.end + linger
            raw.append([w.start, end, pop + " ".join(parts)])
    # one at a time: cut each event off where the next begins
    for k in range(len(raw) - 1):
        raw[k][1] = min(raw[k][1], raw[k + 1][0])
    return [(s, e, t) for s, e, t in raw if e - s >= 0.02]


def hook_text(hook: str, emphasis: str = "", max_line: int = 16) -> str:
    """Hook in our caption style: white, the key word in yellow, wrapped onto up to 3 lines."""
    words = [clean_word(w) for w in hook.split() if clean_word(w)]
    emph = clean_word(emphasis) if emphasis else ""
    lines, cur = [], []
    for w in words:
        if cur and len(" ".join(cur + [w])) > max_line:
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    out = []
    for line in lines:
        out.append(" ".join(r"{\c&H00FFFF&}" + w + r"{\c&HFFFFFF&}" if emph and w.strip("?!") == emph.strip("?!") else w
                            for w in line))
    return r"\N".join(out)


def build_ass(words: list[Word], width: int = 1080, height: int = 1920, font: str = "Arial",
              margin_v: int = 560, center_y: int | None = None, hook: str = "", emphasis: str = "",
              hook_seconds: float = HOOK_SECONDS) -> str:
    """ASS subtitle file. center_y: put captions (and the hook) centred at this height,
    e.g. on the split-screen seam. hook: shown for the first hook_seconds, captions after."""
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
Style: Hook,{font},104,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,9,4,{align},50,50,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    skip = 0.0
    if hook:
        skip = hook_seconds
        anim = r"{\fad(120,150)\fscx90\fscy90\t(0,160,\fscx100\fscy100)}"
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(hook_seconds)},Hook,,0,0,0,,{place}{anim}{hook_text(hook, emphasis)}")
    for s, e, t in caption_events(words, skip_until=skip):
        lines.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{place}{t}")
    return header + "\n".join(lines) + "\n"
