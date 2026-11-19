"""Check (and if needed correct) that caption word times line up with the voice.

A small voice detector (Silero VAD, bundled with faster-whisper, runs offline) finds
exactly when speech starts in the clip's audio. Each caption that starts after a pause
should start when speech starts. If the captions are consistently early or late, every
word is shifted by that amount. The result is logged for every clip.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path

CORRECT_ABOVE = 0.12  # shift captions if they're off by more than this (seconds)
MAX_CORRECTION = 4.0  # never shift by more than this: something else would be wrong


@dataclass
class SyncReport:
    offset: float  # median (caption start - voice start); + means captions are late
    samples: int
    corrected: bool
    outside_speech: float  # share of words that fall where no speech was detected

    def text(self) -> str:
        if self.samples < 3:
            return "[sync] too little speech to check caption timing"
        if self.corrected:
            return (f"[sync] captions were {abs(self.offset):.2f}s {'late' if self.offset > 0 else 'early'}: "
                    f"corrected to match the voice ({self.samples} words checked)")
        return f"[sync] captions match the voice (within {abs(self.offset):.2f}s, {self.samples} words checked)"


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
    words best overlap it. offset > 0 means captions are late. When several shifts fit
    about equally well (long continuous speech), the smallest one is chosen."""
    if len(words) < 3 or not regions:
        return 0.0, len(words), 0.0
    shifts = [round(-search + i * step, 3) for i in range(int(2 * search / step) + 1)]
    scores = {sh: _overlap_score(words, regions, sh) for sh in shifts}
    best = max(scores.values())
    near_best = [sh for sh, sc in scores.items() if sc >= best - 0.01]
    if 0.0 in near_best:  # already fits as well as anything: leave the captions alone
        shift = 0.0
    else:  # middle of the best-fitting stretch around the best shift
        peak = max(scores, key=scores.get)
        i = shifts.index(peak)
        lo = hi = i
        while lo > 0 and shifts[lo - 1] in near_best:
            lo -= 1
        while hi < len(shifts) - 1 and shifts[hi + 1] in near_best:
            hi += 1
        shift = round((shifts[lo] + shifts[hi]) / 2, 3)
    outside = 1.0 - scores[0.0] if 0.0 in scores else 0.0
    return -shift, len(words), outside


def check_and_fix(words, wav: Path, log=print):
    """Returns (words, report). Words are shifted if they're consistently off."""
    from .captions import Word

    try:
        regions = speech_regions(wav)
    except Exception as e:  # noqa: BLE001 - never block a clip on this
        log(f"[sync] couldn't check caption timing: {e}")
        return words, None
    offset, n, outside = measure(words, regions)
    corrected = n >= 3 and CORRECT_ABOVE < abs(offset) <= MAX_CORRECTION
    if corrected:
        words = [Word(max(0.0, w.start - offset), max(0.0, w.end - offset), w.text) for w in words]
    report = SyncReport(round(offset, 3), n, corrected, round(outside, 2))
    log(report.text())
    if n >= 3 and abs(offset) > MAX_CORRECTION:
        log(f"[sync] WARNING: captions look {offset:+.1f}s off, too much to correct safely")
    return words, report
