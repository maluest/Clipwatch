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


def test_drift_during_the_clip_is_corrected():
    from clipwatch.sync import fix_words
    regions = [(s + k * 24, e + k * 24) for k in range(2) for s, e in REGIONS]  # ~45s of talking
    words = []
    for s, e in regions:
        t = s
        while t + 0.3 <= e:
            words.append(Word(t, t + 0.28, "w"))
            t += 0.32
    truth = [w.start for w in words]
    # captions drift: on time at the start, 0.8s early by the end (like squeezed-out audio holes)
    end = max(truth)
    drifted = [Word(w.start - 0.8 * w.start / end, w.end - 0.8 * w.start / end, "w") for w in words]
    fixed, rep = fix_words(drifted, regions)
    errs = [abs(f.start - t) for f, t in zip(fixed, truth)]
    before = [abs(d.start - t) for d, t in zip(drifted, truth)]
    assert max(before) > 0.7
    assert sum(errs) / len(errs) < 0.12, f"mean error after fixing {sum(errs) / len(errs):.2f}s"
    assert any(w[3] for w in rep.windows)


def test_report_text_mentions_windows():
    from clipwatch.sync import fix_words
    _, rep = fix_words(_words(0.0), REGIONS)
    assert "in sync" in rep.text() and "words checked" in rep.text()
