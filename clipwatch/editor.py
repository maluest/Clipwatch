"""Turn a stretch of stream into a finished vertical short (1080x1920):
layout (split screen with the streamer on top / blurred background / full crop),
levelled sound, a hook title for the first 3 seconds, burned-in captions."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import captions as cap
from .facecam import detect_cam
from .tools import ffmpeg_exe, has_audio

W, H = 1080, 1920
# Live streams have holes in the audio (skipped ad segments, dropped packets). A WAV file
# has no timestamps, so without this the holes get squeezed out and every word after a
# hole is heard too early: captions drift ahead of the voice. This fills holes with
# silence (and trims overlaps) so the speech model and the finished clip share one timeline.
AUDIO_TIMELINE = "aresample=async=1:first_pts=0"
CAM_H = 760  # split screen: streamer panel height on top; the game fills the rest


@dataclass
class EditOptions:
    layout: str = "auto"  # "auto" | "facecam" | "fit" | "crop"
    cam: tuple[float, float, float, float] | None = None  # facecam box as fractions: x, y, w, h
    captions: bool = True
    whisper_model: str = "auto"  # large-v3-turbo on an NVIDIA GPU, small on the CPU
    device: str = "auto"
    font: str = "Arial"
    speech_hint: str = ""  # streamer name, game etc., helps the speech model spell them right
    cam_memory: Path | None = None  # file remembering where this channel's webcam was last time


def parse_cam(text: str) -> tuple[float, float, float, float]:
    """'0.75,0.05,0.22,0.25' -> box. Values are fractions of the video's width/height."""
    vals = [float(v) for v in text.split(",")]
    if len(vals) != 4 or not all(0 <= v <= 1 for v in vals) or vals[0] + vals[2] > 1.001 or vals[1] + vals[3] > 1.001:
        raise ValueError("--cam must be four numbers between 0 and 1: x,y,width,height (e.g. 0.75,0.05,0.22,0.25)")
    return vals[0], vals[1], vals[2], vals[3]


def game_crop_x(cam, frame_w: int, frame_h: int, game_h: int) -> int:
    """Left edge (pixels) of the 1080-wide game window in the bottom panel. Normally centred;
    slid sideways (up to 12% of the picture) so the streamer's webcam overlay doesn't
    leak into the edge of the game half."""
    sw = int(round(game_h * frame_w / frame_h / 2) * 2)  # game picture width after scaling
    if sw <= W:
        return 0
    ww = W / sw  # window width as a fraction of the picture
    left = 0.5 - ww / 2
    if cam:
        x, _y, w, _h = cam
        avoid_l, avoid_r = x - w, x + 2 * w  # the overlay is usually wider than the face crop
        right = left + ww
        if x + w / 2 > 0.5 and avoid_l < right:  # webcam on the right
            left -= min(right - avoid_l, 0.12, left)
        elif x + w / 2 < 0.5 and avoid_r > left:  # webcam on the left
            left += min(avoid_r - left, 0.12, 1 - ww - left)
    return max(0, min(int(round(left * sw)), sw - W))


def video_filter(layout: str, cam=None, frame_size: tuple[int, int] = (1920, 1080)) -> str:
    if layout == "crop":
        return f"[0:v]scale=-2:{H},crop={W}:{H}[v0]"
    if layout == "facecam":
        if not cam:
            raise ValueError("The facecam layout needs --cam x,y,w,h (where the webcam is in the stream)")
        x, y, w, h = cam
        cam_h, game_h = CAM_H, H - CAM_H
        gx = game_crop_x(cam, frame_size[0], frame_size[1], game_h)
        return (
            f"[0:v]split[a][b];"
            f"[a]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale={W}:{cam_h}:force_original_aspect_ratio=increase,crop={W}:{cam_h}[cam];"
            f"[b]scale=-2:{game_h},crop={W}:{game_h}:{gx}:0[game];"
            f"[cam][game]vstack[v0]"
        )
    # fit: whole picture in the middle, blurred zoomed copy behind it
    return (
        f"[0:v]split[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:2,eq=brightness=-0.12[bg];"
        f"[b]scale={W}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]"
    )


def timed_transcript(words) -> str:
    """'[0:03] words words...' lines, a new line every ~4 seconds, for the hook writer."""
    lines, cur, t0 = [], [], None
    for w in words or []:
        if t0 is None:
            t0 = w.start
        if cur and w.start - t0 >= 4:
            lines.append(f"[{int(t0) // 60}:{int(t0) % 60:02d}] " + " ".join(cur))
            cur, t0 = [], w.start
        cur.append(w.text)
    if cur:
        lines.append(f"[{int(t0) // 60}:{int(t0) % 60:02d}] " + " ".join(cur))
    return "\n".join(lines)


def _input_args(source: Path, start: float, duration: float) -> list[str]:
    src = str(Path(source).resolve())
    pre = ["-f", "concat", "-safe", "0"] if src.endswith(".txt") else []
    return [*pre, "-i", src, "-ss", f"{start:.3f}", "-t", f"{duration:.3f}"]


def _run(cmd: list[str], cwd: Path) -> None:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + r.stderr[-2500:])


def make_short(source: Path, start: float, duration: float, out_dir: Path, opts: EditOptions,
               words: list[cap.Word] | None = None, log=print,
               hook_fn: Callable[[list[str], float, str], dict | None] | None = None) -> Path:
    """source: a video file, or a concat .txt list of stream segments.
    words: pre-made captions (skips speech-to-text; used by tests).
    hook_fn(input_args, duration, timed_transcript) -> {"hook", "emphasis", "caption", "hashtags", "summary"}"""
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    ff = ffmpeg_exe()
    first_input = Path(source)
    if first_input.suffix == ".txt":
        first_input = Path(first_input.read_text(encoding="utf-8").splitlines()[0][6:-1])
    audio = has_audio(str(first_input))
    from .tools import video_size
    fw, fh = video_size(str(first_input))

    layout, cam = opts.layout, opts.cam
    if layout in ("auto", "facecam") and not cam:
        log("[edit] looking for the streamer's webcam")
        cam = detect_cam(_input_args(source, start, duration), duration, fw / fh, W / CAM_H, log,
                         memory=opts.cam_memory)
        if cam:
            log(f"[edit] split screen: webcam found at {','.join(str(v) for v in cam)} "
                "(if the top crop is off, set it yourself with --cam)")
            (out_dir / "cam.txt").write_text(",".join(str(v) for v in cam) + "\n", encoding="utf-8")
        elif layout == "facecam":
            raise ValueError("Couldn't find the streamer's webcam in this clip. Set it with --cam x,y,w,h")
        else:
            log("[edit] no webcam found in this clip: using the full-picture layout")
    if layout == "auto":
        layout = "facecam" if cam else "fit"
    vf = video_filter(layout, cam, (fw, fh))
    if opts.captions and audio and words is None:
        wav = out_dir / "audio.wav"
        _run([ff, "-y", "-hide_banner", "-loglevel", "error", *_input_args(source, start, duration),
              "-vn", "-af", AUDIO_TIMELINE, "-ac", "1", "-ar", "16000", str(wav)], out_dir)
        log("[edit] writing captions (listening to the clip)")
        words = cap.transcribe(wav, opts.whisper_model, opts.device, log, hint=opts.speech_hint)
        log(f"[edit] captions done ({len(words)} words)")
        from .sync import check_and_fix
        words, report = check_and_fix(words, wav, log)
        if report:
            (out_dir / "sync.json").write_text(json.dumps(report.__dict__), encoding="utf-8")
        wav.unlink(missing_ok=True)
    if not (opts.captions and audio):
        words = []
    if words:
        (out_dir / "transcript.txt").write_text(timed_transcript(words) + "\n", encoding="utf-8")

    hook = None
    if hook_fn:
        hook = hook_fn(_input_args(source, start, duration), duration, timed_transcript(words))
        if hook:
            (out_dir / "hook.json").write_text(json.dumps(hook, indent=2, ensure_ascii=False), encoding="utf-8")
            post = hook.get("caption", "").strip() + "\n\n" + " ".join(hook.get("hashtags", []))
            (out_dir / "post.txt").write_text(post.strip() + "\n", encoding="utf-8")

    if words or hook:
        ass = cap.build_ass(words, W, H, opts.font, center_y=CAM_H if layout == "facecam" else None,
                            hook=(hook or {}).get("hook", ""), emphasis=(hook or {}).get("emphasis", ""))
        (out_dir / "captions.ass").write_text(ass, encoding="utf-8")
        # relative filename: avoids Windows drive-letter escaping problems inside the filter
        vf += ";[v0]subtitles=captions.ass[v1]"
        vout = "[v1]"
    else:
        vout = "[v0]"
    vf += f";{vout}setsar=1,format=yuv420p[vout]"

    out = out_dir / "clip.mp4"
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", *_input_args(source, start, duration),
           "-filter_complex", vf, "-map", "[vout]"]
    if audio:
        cmd += ["-map", "0:a:0", "-af", AUDIO_TIMELINE + ",loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000",
                "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-fpsmax", "60",
            "-movflags", "+faststart", str(out)]
    log("[edit] rendering vertical clip")
    _run(cmd, out_dir)
    return out
