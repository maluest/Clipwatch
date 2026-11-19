"""Write the hook title, post caption and hashtags for a clip with Claude.

Claude is shown what happened in the clip: ~6 frames spread across it (game picture,
streamer's face, in-game subtitles), the transcript with timings, and how chat reacted.
It writes the on-screen hook (first 3 seconds), the caption to post with the clip,
hashtags, and a one-line summary.

Needs an Anthropic API key: either the ANTHROPIC_API_KEY environment variable, or a
file called anthropic_key.txt in the clipwatch folder (install.bat creates it).
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path

from .tools import ffmpeg_exe

MODEL = "claude-opus-5-5"
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


def write_hook(input_args: list[str], duration: float, transcript: str, chat: list[str],
               streamer: str, stream_title: str = "", effort: str = "medium", log=print) -> dict | None:
    """Returns {"summary", "hook", "emphasis", "caption", "hashtags"}, or None if it couldn't."""
    key = api_key()
    if not key:
        log("[hook] no Anthropic API key (anthropic_key.txt): making the clip without a hook")
        return None
    import anthropic

    try:
        with tempfile.TemporaryDirectory(prefix="clipwatch_hook_") as tmp:
            frames = grab_frames(input_args, duration, Path(tmp))
            content = []
            for i, f in enumerate(frames):
                content.append({"type": "text", "text": f"Frame {i + 1} of {len(frames)}:"})
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(f.read_bytes()).decode()}})
        chat_text = "\n".join(c for c in chat[-40:] if c.strip()) or "(no chat captured)"
        content.append({"type": "text", "text": (
            f"Streamer: {streamer}\n"
            + (f"Stream title: {stream_title}\n" if stream_title else "")
            + f"Clip length: {duration:.0f} seconds\n\n"
            f"Transcript of the streamer (timestamps from the start of the clip):\n{transcript or '(nothing said)'}\n\n"
            f"Chat when it exploded:\n{chat_text}"
        )})

        client = anthropic.Anthropic(api_key=key)
        log("[hook] Claude is writing the hook...")
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
        text = next((b.text for b in resp.content if b.type == "text"), "")
        out = json.loads(text)
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
    except (ValueError, subprocess.CalledProcessError) as e:
        log(f"[hook] couldn't write a hook: {e}")
        return None

    out["hook"] = _clean_hook(out.get("hook", ""))
    out["hashtags"] = ["#" + h.lstrip("#").replace(" ", "") for h in out.get("hashtags", []) if h.strip()]
    if not out["hook"]:
        return None
    u = resp.usage
    log(f"[hook] \"{out['hook']}\"  ({u.input_tokens} in / {u.output_tokens} out tokens)")
    return out
