"""Turn a stretch of stream into a finished vertical short (1080x1920):
layout (split screen with the streamer on top / blurred background / full crop),
levelled sound, burned-in captions."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import captions as cap
from .facecam import detect_cam
from .tools import ffmpeg_exe, has_audio

W, H = 1080, 1920
CAM_H = 760  # split screen: streamer panel height on top; the game fills the rest


@dataclass
class EditOptions:
    layout: str = "auto"  # "auto" | "facecam" | "fit" | "crop"
    cam: tuple[float, float, float, float] | None = None  # facecam box as fractions: x, y, w, h
    captions: bool = True
    whisper_model: str = "small"
    device: str = "auto"
    font: str = "Arial"
    cam_memory: Path | None = None  # file remembering where this channel's webcam was last time


def parse_cam(text: str) -> tuple[float, float, float, float]:
    """'0.75,0.05,0.22,0.25' -> box. Values are fractions of the video's width/height."""
    vals = [float(v) for v in text.split(",")]
    if len(vals) != 4 or not all(0 <= v <= 1 for v in vals) or vals[0] + vals[2] > 1.001 or vals[1] + vals[3] > 1.001:
        raise ValueError("--cam must be four numbers between 0 and 1: x,y,width,height (e.g. 0.75,0.05,0.22,0.25)")
    return vals[0], vals[1], vals[2], vals[3]


def video_filter(layout: str, cam=None) -> str:
    if layout == "crop":
        return f"[0:v]scale=-2:{H},crop={W}:{H}[v0]"
    if layout == "facecam":
        if not cam:
            raise ValueError("The facecam layout needs --cam x,y,w,h (where the webcam is in the stream)")
        x, y, w, h = cam
        cam_h, game_h = CAM_H, H - CAM_H
        return (
            f"[0:v]split[a][b];"
            f"[a]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale={W}:{cam_h}:force_original_aspect_ratio=increase,crop={W}:{cam_h}[cam];"
            f"[b]scale=-2:{game_h},crop={W}:{game_h}[game];"
            f"[cam][game]vstack[v0]"
        )
    # fit: whole picture in the middle, blurred zoomed copy behind it
    return (
        f"[0:v]split[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:2,eq=brightness=-0.12[bg];"
        f"[b]scale={W}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]"
    )


def _input_args(source: Path, start: float, duration: float) -> list[str]:
    src = str(Path(source).resolve())
    pre = ["-f", "concat", "-safe", "0"] if src.endswith(".txt") else []
    return [*pre, "-i", src, "-ss", f"{start:.3f}", "-t", f"{duration:.3f}"]


def _run(cmd: list[str], cwd: Path) -> None:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + r.stderr[-2500:])


def make_short(source: Path, start: float, duration: float, out_dir: Path, opts: EditOptions,
               words: list[cap.Word] | None = None, log=print) -> Path:
    """source: a video file, or a concat .txt list of stream segments.
    words: pre-made captions (skips speech-to-text; used by tests)."""
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    ff = ffmpeg_exe()
    first_input = Path(source)
    if first_input.suffix == ".txt":
        first_input = Path(first_input.read_text(encoding="utf-8").splitlines()[0][6:-1])
    audio = has_audio(str(first_input))

    layout, cam = opts.layout, opts.cam
    if layout in ("auto", "facecam") and not cam:
        from .tools import video_size
        fw, fh = video_size(str(first_input))
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
    vf = video_filter(layout, cam)
    if opts.captions and audio:
        if words is None:
            wav = out_dir / "audio.wav"
            _run([ff, "-y", "-hide_banner", "-loglevel", "error", *_input_args(source, start, duration),
                  "-vn", "-ac", "1", "-ar", "16000", str(wav)], out_dir)
            log("[edit] writing captions (listening to the clip)")
            words = cap.transcribe(wav, opts.whisper_model, opts.device, log)
            log(f"[edit] captions done ({len(words)} words)")
            wav.unlink(missing_ok=True)
        (out_dir / "captions.ass").write_text(cap.build_ass(words, W, H, opts.font, center_y=CAM_H if layout == "facecam" else None), encoding="utf-8")
        (out_dir / "transcript.txt").write_text(" ".join(w.text for w in words) + "\n", encoding="utf-8")
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
        cmd += ["-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000",
                "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-fpsmax", "60",
            "-movflags", "+faststart", str(out)]
    log("[edit] rendering vertical clip")
    _run(cmd, out_dir)
    return out
