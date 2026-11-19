"""Write the hook title, post caption and hashtags for a clip with Claude.

Claude is shown what happened in the clip: ~6 frames spread across it (game picture,
streamer's face, in-game subtitles), the transcript with timings, and how chat reacted.
It writes the on-screen hook (first 3 seconds), the caption to post with the clip,
hashtags, and a one-line summary.

How Claude is reached, in order:
1. Claude Code (the `claude` command) logged in with your Claude subscription. No API
   key, no extra cost; it uses your plan's usage limits. This is the default.
2. Only if Claude Code isn't installed: an Anthropic API key (ANTHROPIC_API_KEY or
   anthropic_key.txt in the clipwatch folder), billed per use.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .tools import ffmpeg_exe

MODEL = "claude-opus-5-5"  # API route
CLAUDE_CODE_MODEL = "opus"  # Claude Code route (alias for the current Opus)
CLAUDE_CODE_TIMEOUT = 300
FRAMES = 6
KEY_FILE = Path(__file__).resolve().parent.parent / "anthropic_key.txt"

SYSTEM = """You write the on-screen hook and the post text for short vertical clips cut from live streams \
(mostly gaming streams, often GTA 6). The clip will be posted on TikTok, YouTube Shorts and Instagram Reels.

You get: frames from across the clip in order (the streamer's webcam is usually on top, the game below; \
in-game subtitles may be visible), the transcript of what the streamer said with timestamps, and the chat \
messages from the moment chat exploded (that reaction is why this clip was cut).

First work out what actually happened: what is happening in the game, what the streamer did or said, and \
why chat reacted. The hook must come from that. Never invent events, quotes or outcomes that the frames, \
transcript or chat don't show.

THE HOOK (shown big on screen for the first 3 seconds, before captions start)
Its only job is to stop the scroll and make people watch to the end.
- Name who it's about: the streamer by name, or the game character if that's the story.
- Say the specific, surprising thing. Concrete nouns and actions, not vague words like "crazy moment" or "insane".
- Open a curiosity gap: set up the moment without giving away the punchline or the ending.
- Sound like a person talking: plain words, past or present tense.
- 4 to 9 words, at most 45 characters. Must be readable in under 2 seconds.
- No emoji, no hashtags, no quotation marks, no ALL CAPS words (it is upper-cased automatically).
- No clickbait that the clip doesn't pay off, and nothing degrading or sexual about real people.
Good examples of the shape: "Speed found out you can doom scroll on GTA 6", \
"GTA 6 NPCs went from bar fight to morning cuddle", "Joe wasn't ready for this GTA 6 hangover scene".

EMPHASIS: pick the single word from the hook that carries the surprise; it is shown in yellow.

POST CAPTION: 1-2 short sentences in the same voice, adding context the hook leaves out, and ending with a \
question or nudge that invites comments. 0-2 emoji allowed here.

HASHTAGS: 4-6, most relevant first: the game, the streamer (if named), what happens in the clip, then one \
broad one like gaming. No spaces inside a hashtag.

SUMMARY: one plain sentence saying what happens in the clip, for the editor's records."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "hook": {"type": "string"},
        "emphasis": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "hook", "emphasis", "caption", "hashtags"],
    "additionalProperties": False,
}


def api_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key.strip()
    try:
        if KEY_FILE.exists():
            return KEY_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    return None


def grab_frames(input_args: list[str], duration: float, outdir: Path) -> list[Path]:
    """FRAMES evenly spaced JPEG frames, 768 px wide (plenty to read the scene and subtitles)."""
    fps = FRAMES / max(duration, 1.0)
    subprocess.run(
        [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", *input_args,
         "-vf", f"fps={fps:.4f},scale=768:-2", "-frames:v", str(FRAMES), "-q:v", "4", str(outdir / "h_%02d.jpg")],
        capture_output=True, check=True,
    )
    return sorted(outdir.glob("h_*.jpg"))


def _clean_hook(text: str) -> str:
    text = " ".join(text.replace('"', "").replace("“", "").replace("”", "").split())
    return text.rstrip(".")


def _context(duration: float, transcript: str, chat: list[str], streamer: str, stream_title: str) -> str:
    chat_text = "\n".join(c for c in chat[-40:] if c.strip()) or "(no chat captured)"
    return (f"Streamer: {streamer}\n"
            + (f"Stream title: {stream_title}\n" if stream_title else "")
            + f"Clip length: {duration:.0f} seconds\n\n"
            f"Transcript of the streamer (timestamps from the start of the clip):\n{transcript or '(nothing said)'}\n\n"
            f"Chat when it exploded:\n{chat_text}")


def _finish(out: dict, log, note: str = "") -> dict | None:
    out["hook"] = _clean_hook(str(out.get("hook", "")))
    out["emphasis"] = str(out.get("emphasis", ""))
    out["caption"] = str(out.get("caption", ""))
    out["summary"] = str(out.get("summary", ""))
    out["hashtags"] = ["#" + str(h).lstrip("#").replace(" ", "") for h in out.get("hashtags", []) if str(h).strip()]
    if not out["hook"]:
        log("[hook] Claude didn't return a hook: making the clip without one")
        return None
    log(f"[hook] \"{out['hook']}\"{note}")
    return out


def claude_code_exe() -> str | None:
    return shutil.which("claude")


def _parse_json_object(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON in Claude's answer")
    return json.loads(m.group(0))


def _via_claude_code(exe: str, frames: list[Path], context: str, log) -> dict | None:
    """Run Claude Code headless (`claude -p`) on the user's subscription login."""
    frame_list = "\n".join(f"- {p.name}" for p in frames)
    prompt = (
        SYSTEM
        + "\n\n---\nThe frames are image files in the current folder. Use the Read tool to look at each "
        "of them, in this order, before writing anything:\n" + frame_list
        + "\n\n" + context
        + "\n\nWhen done, reply with ONLY a JSON object (no other text) with exactly these keys: "
        '"summary" (string), "hook" (string), "emphasis" (string), "caption" (string), "hashtags" (array of strings).'
    )
    cmd = [exe, "-p", "--model", CLAUDE_CODE_MODEL, "--output-format", "json",
           "--tools", "Read", "--allowedTools", "Read", "--permission-mode", "dontAsk",
           "--max-turns", "12", "--no-session-persistence"]
    # The schema contains quotes; only pass it to a real .exe (a .cmd wrapper would mangle it).
    if not exe.lower().endswith((".cmd", ".bat")):
        cmd += ["--json-schema", json.dumps(SCHEMA, separators=(",", ":"))]
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # make Claude Code use the subscription login
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    log("[hook] Claude (via Claude Code, your subscription) is writing the hook...")
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=str(frames[0].parent) if frames else None, env=env,
                           timeout=CLAUDE_CODE_TIMEOUT, creationflags=flags)
    except subprocess.TimeoutExpired:
        log("[hook] Claude Code took too long: making the clip without a hook")
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        hint = msg[-1][:200] if msg else f"exit code {r.returncode}"
        if any(w in hint.lower() for w in ("login", "log in", "authenticat", "/login")):
            hint += "  ->  open a terminal, run `claude` once and log in"
        log(f"[hook] Claude Code failed: {hint}")
        return None
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        detail = str(data.get("result") or data.get("subtype") or "error")[:200]
        if "limit" in detail.lower():
            log(f"[hook] your Claude usage limit is reached for now ({detail}): clip made without a hook")
        else:
            log(f"[hook] Claude Code couldn't finish: {detail}")
        return None
    out = data.get("structured_output")
    if not isinstance(out, dict):
        try:
            out = _parse_json_object(str(data.get("result", "")))
        except ValueError as e:
            log(f"[hook] couldn't read Claude's answer: {e}")
            return None
    return _finish(out, log)


def _via_api(key: str, frames: list[Path], context: str, effort: str, log) -> dict | None:
    import anthropic

    content = []
    for i, f in enumerate(frames):
        content.append({"type": "text", "text": f"Frame {i + 1} of {len(frames)}:"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(f.read_bytes()).decode()}})
    content.append({"type": "text", "text": context})
    log("[hook] Claude (API key) is writing the hook...")
    try:
        client = anthropic.Anthropic(api_key=key)
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        if resp.stop_reason == "refusal":
            log("[hook] Claude declined this clip: making it without a hook")
            return None
        out = json.loads(next((b.text for b in resp.content if b.type == "text"), ""))
    except anthropic.AuthenticationError:
        log("[hook] the Anthropic API key was rejected: check anthropic_key.txt")
        return None
    except anthropic.PermissionDeniedError as e:
        log(f"[hook] Anthropic refused the request (credit or permissions?): {e.message}")
        return None
    except anthropic.RateLimitError:
        log("[hook] Anthropic rate limit hit: making this clip without a hook")
        return None
    except anthropic.APIStatusError as e:
        log(f"[hook] Anthropic error {e.status_code}: {e.message}")
        return None
    except anthropic.APIConnectionError:
        log("[hook] couldn't reach Anthropic (internet?): making the clip without a hook")
        return None
    except ValueError as e:
        log(f"[hook] couldn't read Claude's answer: {e}")
        return None
    u = resp.usage
    return _finish(out, log, f"  ({u.input_tokens} in / {u.output_tokens} out tokens)")


def write_hook(input_args: list[str], duration: float, transcript: str, chat: list[str],
               streamer: str, stream_title: str = "", effort: str = "medium", log=print) -> dict | None:
    """Returns {"summary", "hook", "emphasis", "caption", "hashtags"}, or None if it couldn't."""
    exe = claude_code_exe()
    key = None if exe else api_key()
    if not exe and not key:
        log("[hook] Claude Code isn't installed (and no API key): making the clip without a hook. "
            "Install it with:  irm https://claude.ai/install.ps1 | iex   then run `claude` once to log in.")
        return None
    context = _context(duration, transcript, chat, streamer, stream_title)
    with tempfile.TemporaryDirectory(prefix="clipwatch_hook_") as tmp:
        try:
            frames = grab_frames(input_args, duration, Path(tmp))
        except subprocess.CalledProcessError as e:
            log(f"[hook] couldn't take frames from the clip: {e}")
            return None
        if exe:
            return _via_claude_code(exe, frames, context, log)
        return _via_api(key, frames, context, effort, log)


def check_claude_code(log=print) -> bool:
    """Quick test that Claude Code is installed and logged in (uses a tiny bit of your plan)."""
    exe = claude_code_exe()
    if not exe:
        log("Claude Code isn't installed. In PowerShell run:  irm https://claude.ai/install.ps1 | iex")
        return False
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    r = subprocess.run([exe, "-p", "--model", CLAUDE_CODE_MODEL, "--output-format", "json", "--tools", "",
                        "--max-turns", "1", "--no-session-persistence"],
                       input="Reply with just the word OK.", capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=120)
    try:
        data = json.loads(r.stdout)
        ok = not data.get("is_error") and "OK" in str(data.get("result", "")).upper()
    except ValueError:
        ok = False
    if ok:
        log(f"Claude Code works and is logged in ({exe}). Hooks will use your Claude subscription.")
    else:
        log("Claude Code is installed but didn't answer. Open a terminal, run `claude`, log in, then try again.\n"
            + (r.stderr or r.stdout or "").strip()[-400:])
    return ok
