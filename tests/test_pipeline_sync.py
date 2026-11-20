"""End-to-end: captions must appear when the voice plays in the FINISHED clip, even when
the stream has audio holes and a reconnect (timestamps restarting). A beep stands in for
each spoken word; a stand-in transcriber "hears" the beeps in the audio it is given."""
import re
import shutil
import subprocess

import numpy as np
import pytest

from clipwatch import captions as cap
from clipwatch.editor import EditOptions, make_short

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _env_onsets(raw: bytes, rate: int):
    a = np.frombuffer(raw, np.int16).astype(np.float32)
    block = rate // 100
    a = a[: len(a) // block * block]
    env = np.abs(a).reshape(-1, block).mean(1)
    return [round(i * 0.01, 2) for i in np.where((env[1:] > 2000) & (env[:-1] <= 2000))[0] + 1]


def _played_beeps(path):  # where beeps are heard when the clip plays (timestamps respected)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-af", "aresample=async=1:first_pts=0",
                          "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True).stdout
    return _env_onsets(raw, 8000)


def _piece(path, seconds, extra_af=None):
    af = ["-af", extra_af] if extra_af else []
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=0x334455:s=640x360:r=30",
                    "-f", "lavfi", "-i", "sine=f=1000:r=48000,volume=enable='gte(mod(t+1.5,3),0.12)':volume=0",
                    "-t", str(seconds), *af, "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
                    "-c:a", "aac", "-f", "mpegts", str(path)], check=True)


def test_captions_land_on_the_voice_in_the_finished_clip(tmp_path, monkeypatch):
    a, b, c = tmp_path / "a.ts", tmp_path / "b.ts", tmp_path / "c.ts"
    _piece(a, 10)
    _piece(b, 10, "aselect='not(between(t,4,6))'")  # 2s hole in the audio
    _piece(c, 10)  # encoded separately: its timestamps restart at 0, like a reconnect
    lst = tmp_path / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in (a, b, c)))

    def fake_transcribe(wav, *args, **kwargs):  # "hears" a word at every beep
        raw = (cap.load_wav(wav) * 32768).astype(np.int16).tobytes()
        return [cap.Word(t, t + 0.1, "beep") for t in _env_onsets(raw, 16000)]
    monkeypatch.setattr(cap, "transcribe", fake_transcribe)

    out = make_short(lst, 1.0, 27.0, tmp_path / "out", EditOptions(layout="fit"), log=lambda m: None)
    played = _played_beeps(out)
    ass = (tmp_path / "out" / "captions.ass").read_text()
    starts = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:") and ",Cap," in line:
            h, m, s = line.split(",")[1].split(":")
            starts.append(int(h) * 3600 + int(m) * 60 + float(s))
    caption_starts = sorted(set(round(x, 2) for x in starts))
    assert len(played) >= 7, played
    for p in played:
        nearest = min(caption_starts, key=lambda c: abs(c - p))
        assert abs(nearest - p) <= 0.05, f"beep plays at {p}s, nearest caption starts at {nearest}s"
    assert not (tmp_path / "out" / "base.mp4").exists()
