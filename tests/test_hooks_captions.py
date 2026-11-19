import json
import types

from clipwatch.captions import Word, build_ass, caption_events, clean_word, group_words, hook_text
from clipwatch.editor import game_crop_x, timed_transcript


def _ts(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _events(ass, style):
    out = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            if parts[3] == style:
                out.append((_ts(parts[1]), _ts(parts[2]), parts[9]))
    return out


def test_never_two_captions_at_once_even_when_whisper_overlaps():
    # like the real clip: whisper gave overlapping times around "stand dead still"
    words = [Word(0.0, 0.3, "Like"), Word(0.25, 0.5, "you"), Word(0.45, 0.8, "can't"),
             Word(0.7, 1.0, "stand"), Word(0.95, 1.3, "dead"), Word(1.2, 1.6, "still"),
             Word(1.55, 1.9, "taking"), Word(1.85, 2.0, "a"), Word(1.95, 2.4, "piss.")]
    ev = caption_events(words)
    for (s1, e1, _), (s2, _e2, _) in zip(ev, ev[1:]):
        assert e1 <= s2 + 1e-9, "a caption is still on screen when the next starts"


def test_trailing_punctuation_dropped_questions_kept():
    assert clean_word("Bro,") == "BRO"
    assert clean_word("piss.") == "PISS"
    assert clean_word("what?") == "WHAT?"
    assert clean_word("wait...") == "WAIT"


def test_caption_fits_one_line():
    words = [Word(i * 0.3, i * 0.3 + 0.25, w) for i, w in enumerate("little morning cuddle absolutely".split())]
    for g in group_words(words):
        assert len(" ".join(clean_word(w.text) for w in g)) <= 15 or len(g) == 1


def test_hook_takes_first_three_seconds_then_captions():
    words = [Word(0.5, 0.9, "hello"), Word(2.5, 2.9, "there"), Word(3.2, 3.5, "bro,"), Word(3.6, 4.0, "look")]
    ass = build_ass(words, center_y=760, hook="Joe wasn't ready for this GTA 6 scene", emphasis="ready")
    hook = _events(ass, "Hook")
    caps = _events(ass, "Cap")
    assert hook[0][0] == 0 and abs(hook[0][1] - 3.0) < 0.01
    assert all(s >= 3.0 for s, _, _ in caps)
    assert "HELLO" not in ass and "THERE" not in ass  # said under the hook: not captioned
    assert "BRO" in caps[0][2] and "BRO," not in ass
    assert r"{\c&H00FFFF&}READY{\c&HFFFFFF&}" in hook[0][2]


def test_hook_wraps_to_short_lines():
    text = hook_text("GTA 6 NPCs went from bar fight to morning cuddle", "cuddle")
    lines = text.split(r"\N")
    assert 2 <= len(lines) <= 4
    assert "CUDDLE" in lines[-1]


def test_game_half_slides_away_from_webcam():
    centred = game_crop_x(None, 1920, 1080, 1160)
    right_cam = game_crop_x((0.851, 0.226, 0.149, 0.186), 1920, 1080, 1160)
    left_cam = game_crop_x((0.0, 0.2, 0.15, 0.19), 1920, 1080, 1160)
    sw = round(1160 * 1920 / 1080 / 2) * 2
    assert right_cam < centred < left_cam
    assert 0 <= right_cam and left_cam + 1080 <= sw
    # right edge of the game window stays left of where the overlay starts (0.851 - 0.149)
    assert (right_cam + 1080) / sw <= 0.703


def test_timed_transcript():
    words = [Word(0.2, 0.5, "hey"), Word(1.0, 1.2, "bro"), Word(5.0, 5.3, "what")]
    assert timed_transcript(words) == "[0:00] hey bro\n[0:05] what"


def test_write_hook_without_key_is_skipped(monkeypatch, tmp_path):
    from clipwatch import hooks
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(hooks, "KEY_FILE", tmp_path / "none.txt")
    monkeypatch.setattr(hooks, "claude_code_exe", lambda: None)
    assert hooks.write_hook([], 10, "", [], "joe", log=lambda m: None) is None


def test_api_key_route_when_claude_code_missing(monkeypatch, tmp_path):
    from clipwatch import hooks
    monkeypatch.setattr(hooks, "claude_code_exe", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(hooks, "grab_frames", lambda *a: [])
    answer = {"summary": "Two NPCs wake up cuddling after a bar fight.",
              "hook": '"GTA 6 NPCs went from bar fight to morning cuddle."', "emphasis": "cuddle",
              "caption": "Rockstar really thought of everything 💀 Would you?",
              "hashtags": ["gta6", "#joe bartolozzi", "#gaming"]}
    sent = {}

    class FakeMessages:
        def create(self, **kw):
            sent.update(kw)
            return types.SimpleNamespace(
                stop_reason="end_turn", usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
                content=[types.SimpleNamespace(type="text", text=json.dumps(answer))])

    class FakeClient:
        def __init__(self, api_key):
            self.beta = types.SimpleNamespace(messages=FakeMessages())

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    out = hooks.write_hook([], 45, "[0:00] little morning cuddle", ["KEKW", "NO WAY"], "joe_bartolozzi",
                           log=lambda m: None)
    assert out["hook"] == "GTA 6 NPCs went from bar fight to morning cuddle"
    assert out["hashtags"] == ["#gta6", "#joebartolozzi", "#gaming"]
    assert sent["model"] == "claude-opus-5-5" and sent["fallbacks"] == "default"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    user_text = sent["messages"][0]["content"][-1]["text"]
    assert "little morning cuddle" in user_text and "KEKW" in user_text and "joe_bartolozzi" in user_text


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
prompt = sys.stdin.read()
log = {"argv": sys.argv[1:], "cwd": os.getcwd(), "files": sorted(os.listdir(".")),
       "has_key": "ANTHROPIC_API_KEY" in os.environ, "prompt": prompt}
open(os.environ["FAKE_CLAUDE_LOG"], "w").write(json.dumps(log))
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "limit":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "Claude usage limit reached. Your limit will reset at 5pm"}))
elif mode == "text":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": 'Here you go: {"summary": "s", "hook": "Joe got jumped in GTA 6.", "emphasis": "jumped", "caption": "c", "hashtags": ["gta6"]}'}))
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "",
                      "structured_output": {"summary": "s", "hook": "Joe wasn't ready for this GTA 6 scene",
                                            "emphasis": "ready", "caption": "c", "hashtags": ["#gta6", "joe"]}}))
"""


def _fake_claude(monkeypatch, tmp_path, mode="ok"):
    from clipwatch import hooks
    exe = tmp_path / "claude"
    exe.write_text(FAKE_CLAUDE)
    exe.chmod(0o755)
    log = tmp_path / "call.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-used")
    monkeypatch.setattr(hooks, "claude_code_exe", lambda: str(exe))

    def frames(input_args, duration, outdir):
        out = []
        for i in range(3):
            f = outdir / f"h_{i:02d}.jpg"
            f.write_bytes(b"jpg")
            out.append(f)
        return out
    monkeypatch.setattr(hooks, "grab_frames", frames)
    return hooks, log


def test_subscription_route_via_claude_code(monkeypatch, tmp_path):
    hooks, log = _fake_claude(monkeypatch, tmp_path)
    out = hooks.write_hook([], 45, "[0:00] bro", ["KEKW"], "joe_bartolozzi", log=lambda m: None)
    assert out["hook"] == "Joe wasn't ready for this GTA 6 scene"
    assert out["hashtags"] == ["#gta6", "#joe"]
    call = json.loads(log.read_text())
    argv = call["argv"]
    assert "-p" in argv and argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--tools") + 1] == "Read" and "--json-schema" in argv
    assert call["has_key"] is False, "the API key must not be passed, so the subscription is used"
    assert call["files"] == ["h_00.jpg", "h_01.jpg", "h_02.jpg"]  # runs inside the frames folder
    assert "h_00.jpg" in call["prompt"] and "KEKW" in call["prompt"] and "THE HOOK" in call["prompt"]


def test_subscription_usage_limit_means_no_hook(monkeypatch, tmp_path):
    hooks, _ = _fake_claude(monkeypatch, tmp_path, "limit")
    msgs = []
    assert hooks.write_hook([], 45, "", [], "joe", log=msgs.append) is None
    assert any("usage limit" in m for m in msgs)


def test_answer_as_plain_text_json_is_still_read(monkeypatch, tmp_path):
    hooks, _ = _fake_claude(monkeypatch, tmp_path, "text")
    out = hooks.write_hook([], 45, "", [], "joe", log=lambda m: None)
    assert out["hook"] == "Joe got jumped in GTA 6"
