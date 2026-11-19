"""Captions must line up with the voice even when the stream's audio has holes
(skipped Twitch ad segments, dropped packets). Regression test for captions that drifted
3-6 seconds ahead of the voice."""
import shutil
import subprocess

import numpy as np
import pytest

from clipwatch.editor import AUDIO_TIMELINE, EditOptions, _input_args, make_short

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _beeps(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-af", "aresample=async=1:first_pts=0",
                          "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32)
    a = a[: len(a) // 80 * 80]
    env = np.abs(a).reshape(-1, 80).mean(1)
    return [round(i * 0.01, 1) for i in np.where((env[1:] > 2000) & (env[:-1] <= 2000))[0] + 1]


def _wav_beeps(path):  # what the speech model hears: a WAV has no timestamps
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32)
    a = a[: len(a) // 80 * 80]
    env = np.abs(a).reshape(-1, 80).mean(1)
    return [round(i * 0.01, 1) for i in np.where((env[1:] > 2000) & (env[:-1] <= 2000))[0] + 1]


def test_speech_audio_and_clip_share_one_timeline_despite_audio_holes(tmp_path):
    src = tmp_path / "gap.ts"
    # beep every 3s; a 2-second hole in the audio at 7-9s (timestamps kept, packets missing)
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=320x180:r=30",
                    "-f", "lavfi", "-i", "sine=f=1000:r=48000,volume=enable='gte(mod(t,3),0.1)':volume=0",
                    "-t", "18", "-af", "aselect='not(between(t,7,9))'", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-f", "mpegts", str(src)], check=True)
    wav = tmp_path / "speech.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", *_input_args(src, 1.0, 15.0), "-vn", "-af", AUDIO_TIMELINE,
                    "-ac", "1", "-ar", "16000", str(wav)], check=True)
    clip = make_short(src, 1.0, 15.0, tmp_path / "out", EditOptions(layout="fit", captions=False), words=[],
                      log=lambda m: None)
    heard, played = _wav_beeps(wav), _beeps(clip)
    assert len(heard) == len(played) >= 4
    for h, p in zip(heard, played):
        assert abs(h - p) <= 0.05, f"speech model hears a beep at {h}s but the clip plays it at {p}s"
