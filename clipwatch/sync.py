"""Check (and if needed correct) that caption word times line up with the voice.

Captions are made from the finished clip's own audio, so they share its timeline. This
is the second safety net, for the speech model's own timing errors:

1. A small voice detector (Silero VAD, bundled with faster-whisper, runs offline) finds
   where speech is in the clip's audio.
2. Whole clip: the caption track is slid from -4s to +4s to find where the words best
   overlap the speech. If they're off by more than 0.12s, every word is shifted.
3. Per 10-second window: the same check, within +-1s, so timing that drifts during the
   clip is corrected too. Shifts between windows blend smoothly.

Captions that already fit are never moved. The result is logged for every clip.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

CORRECT_ABOVE = 0.12  # shift captions if they're off by more than this (seconds)
MAX_CORRECTION = 4.0  # never shift by more than this: something else would be wrong
WINDOW = 10.0  # seconds per window for the drift check
WINDOW_SEARCH = 1.0  # how far a window may be off from the whole-clip alignment
MIN_WINDOW_WORDS = 5


@dataclass
class SyncReport:
    offset: float  # whole clip: + means captions were late, - early
    samples: int  # words compared
    corrected: bool
    outside_speech: float  # share of words where no speech was detected (after fixing)
    windows: list = field(default_factory=list)  # [(start, end, extra offset, corrected)]

    def as_dict(self) -> dict:
        return asdict(self)

    def text(self) -> str:
        if self.samples < 3:
            return "[sync] too little speech to check caption timing"
        fixed_windows = [w for w in self.windows if w[3]]
        if self.corrected:
            msg = (f"[sync] captions were {abs(self.offset):.2f}s {'late' if self.offset > 0 else 'early'}: "
                   f"corrected to match the voice")
        else:
            msg = f"[sync] captions match the voice (within {abs(self.offset):.2f}s)"
        if fixed_windows:
            parts = ", ".join(f"{a:.0f}-{b:.0f}s {o:+.2f}s" for a, b, o, _ in fixed_windows)
            msg += f"; drift corrected in {len(fixed_windows)} window(s): {parts}"
        elif self.windows:
            msg += f"; all {len(self.windows)} 10s windows in sync"
        return msg + f" ({self.samples} words checked)"


def speech_regions(wav: Path) -> list[tuple[float, float]]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    from .captions import load_wav
    audio = load_wav(wav)
    opts = VadOptions(threshold=0.5, min_speech_duration_ms=120, min_silence_duration_ms=150, speech_pad_ms=0)
    return [(c["start"] / 16000, c["end"] / 16000) for c in get_speech_timestamps(audio, opts)]


def _overlap_score(words, regions, shift: float) -> float:
    """How much of the (shifted) words' time falls inside detected speech, 0..1."""
    total = inside = 0.0
    j = 0
    for w in words:
        s, e = w.start + shift, w.end + shift
        d = max(e - s, 0.05)
        total += d
        while j < len(regions) and regions[j][1] < s:
            j += 1
        k = j
        while k < len(regions) and regions[k][0] < e:
            inside += max(0.0, min(e, regions[k][1]) - max(s, regions[k][0]))
            k += 1
    return inside / total if total else 0.0


def measure(words, regions, search: float = 4.0, step: float = 0.02) -> tuple[float, int, float]:
    """(offset, number of words compared, share of words outside speech).
    Slides the caption track against the detected speech and finds the shift where the
    words best overlap it. offset > 0 means captions are late. If no shift fits better
    than none, the offset is 0; otherwise the middle of the best-fitting stretch."""
    if len(words) < 3 or not regions:
        return 0.0, len(words), 0.0
    shifts = [round(-search + i * step, 3) for i in range(int(round(2 * search / step)) + 1)]
    scores = {sh: _overlap_score(words, regions, sh) for sh in shifts}
    best = max(scores.values())
    near_best = set(sh for sh, sc in scores.items() if sc >= best - 0.01)
    if 0.0 in near_best:  # already fits as well as anything: leave the captions alone
        shift = 0.0
    else:
        peak = max(scores, key=scores.get)
        i = shifts.index(peak)
        lo = hi = i
        while lo > 0 and shifts[lo - 1] in near_best:
            lo -= 1
        while hi < len(shifts) - 1 and shifts[hi + 1] in near_best:
            hi += 1
        shift = round((shifts[lo] + shifts[hi]) / 2, 3)
    outside = 1.0 - scores.get(0.0, 0.0)
    return -shift, len(words), outside


def _window_offsets(words, regions):
    """Extra offset per 10s window (after the whole-clip correction)."""
    if not words:
        return []
    end = max(w.end for w in words)
    out = []
    t = 0.0
    while t < end:
        ws = [w for w in words if t <= w.start < t + WINDOW]
        if len(ws) >= MIN_WINDOW_WORDS:
            rs = [r for r in regions if r[1] >= t - WINDOW_SEARCH and r[0] <= t + WINDOW + WINDOW_SEARCH]
            off, _, _ = measure(ws, rs, search=WINDOW_SEARCH)
            out.append((t, min(t + WINDOW, end), off, abs(off) > CORRECT_ABOVE))
        t += WINDOW
    return out


def _apply_windows(words, windows):
    """Shift words by their window's offset, blending linearly between window centres."""
    from .captions import Word

    pts = [((a + b) / 2, o if fixed else 0.0) for a, b, o, fixed in windows]
    if not any(o for _, o in pts):
        return words

    def off_at(t: float) -> float:
        if t <= pts[0][0]:
            return pts[0][1]
        if t >= pts[-1][0]:
            return pts[-1][1]
        for (t0, o0), (t1, o1) in zip(pts, pts[1:]):
            if t0 <= t <= t1:
                return o0 + (o1 - o0) * (t - t0) / (t1 - t0)
        return 0.0

    return [Word(max(0.0, w.start - off_at(w.start)), max(0.0, w.end - off_at(w.start)), w.text) for w in words]


def fix_words(words, regions):
    """Pure version of check_and_fix (no audio reading): returns (words, report)."""
    from .captions import Word

    offset, n, _ = measure(words, regions)
    corrected = n >= 3 and CORRECT_ABOVE < abs(offset) <= MAX_CORRECTION
    if corrected:
        words = [Word(max(0.0, w.start - offset), max(0.0, w.end - offset), w.text) for w in words]
    windows = _window_offsets(words, regions) if n >= 3 else []
    words = _apply_windows(words, windows)
    outside = 1.0 - _overlap_score(words, regions, 0.0) if words and regions else 0.0
    report = SyncReport(round(offset, 3), n, corrected, round(outside, 2),
                        [(round(a, 1), round(b, 1), round(o, 3), f) for a, b, o, f in windows])
    return words, report


def check_and_fix(words, wav: Path, log=print):
    """Returns (words, report). Words are shifted where they're off from the voice."""
    try:
        regions = speech_regions(wav)
    except Exception as e:  # noqa: BLE001 - never block a clip on this
        log(f"[sync] couldn't check caption timing: {e}")
        return words, None
    offset, n, _ = measure(words, regions)
    if n >= 3 and abs(offset) > MAX_CORRECTION:
        log(f"[sync] WARNING: captions look {offset:+.1f}s off, too much to correct safely")
    words, report = fix_words(words, regions)
    log(report.text())
    return words, report
