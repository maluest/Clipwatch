from clipwatch.captions import Word
from clipwatch.sync import measure

# speech detected in phrases, like a streamer talking with pauses
REGIONS = [(1.0, 2.6), (4.0, 5.1), (7.5, 9.8), (12.0, 12.9), (15.2, 17.0), (19.5, 21.0)]


def _words(shift=0.0):
    out = []
    for s, e in REGIONS:
        t = s
        while t + 0.3 <= e:
            out.append(Word(t + shift, t + 0.28 + shift, "w"))
            t += 0.32
    return out


def test_in_sync_captions_are_left_alone():
    offset, n, _ = measure(_words(0.0), REGIONS)
    assert offset == 0.0 and n > 10


def test_early_and_late_captions_are_measured():
    for shift in (-3.0, -1.2, -0.4, 0.3, 1.5):
        offset, _, _ = measure(_words(shift), REGIONS)
        assert abs(offset - shift) <= 0.1, f"moved {shift}s but measured {offset}s"


def test_load_wav_reads_clipwatch_audio(tmp_path):
    import wave

    import numpy as np

    from clipwatch.captions import load_wav
    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes((np.sin(np.arange(16000) / 5) * 10000).astype(np.int16).tobytes())
    a = load_wav(p)
    assert a.dtype == np.float32 and len(a) == 16000 and 0.2 < abs(a).max() <= 1.0
