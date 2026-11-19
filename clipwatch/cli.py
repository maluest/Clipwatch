"""clipwatch command line.

  python -m clipwatch watch <stream link>        watch a live stream and auto-clip hype moments
  python -m clipwatch edit <video> --start 60    turn part of any video file into a vertical short
"""
from __future__ import annotations

import argparse
import json
import queue
import shutil
import signal
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from .chat import ReplayChat, make_chat_reader
from .detector import AudioSpikeDetector, ChatSpikeDetector, Spike
from .editor import EditOptions, make_short, parse_cam
from .platforms import Stream, parse_stream_url
from .recorder import Recorder, mean_volume_db, write_concat_list


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def _edit_options(a) -> EditOptions:
    return EditOptions(
        layout=a.layout,
        cam=parse_cam(a.cam) if a.cam else None,
        captions=not a.no_captions,
        whisper_model=a.model,
        device=a.device,
    )


def _suggest_title(lines: list[str]) -> str:
    words = Counter()
    for line in lines:
        for w in line.split():
            if len(w) > 1:
                words[w.strip(".,!?").upper()] += 1
    common = [w for w, _ in words.most_common(3)]
    return " ".join(common) if common else ""


# ---------------------------------------------------------------- watch -------
def cmd_watch(a) -> int:
    opts = _edit_options(a)
    if a.source_file:
        stream = Stream("file", a.source_file, Path(a.source_file).stem)
    else:
        stream = parse_stream_url(a.url)
    out_root = Path(a.out) / stream.channel
    buffer_dir = out_root / ".buffer"
    out_root.mkdir(parents=True, exist_ok=True)

    chat_det = ChatSpikeDetector(multiplier=a.sensitivity, min_messages=a.min_messages, cooldown=a.cooldown)
    audio_det = AudioSpikeDetector(jump_db=a.audio_jump, cooldown=a.cooldown)
    jobs: "queue.Queue[Spike]" = queue.Queue()
    clip_count = 0

    def on_message(t: float, text: str) -> None:
        chat_det.add_message(t, text)

    reader = None
    if a.chat_replay:
        reader = ReplayChat(a.chat_replay, on_message, log)
    elif not a.no_chat:
        reader = make_chat_reader(stream.platform, stream.channel, stream.url, on_message, log, a.kick_key)

    def use_audio() -> bool:
        return a.audio_too or reader is None or not reader.connected

    def on_segment(seg) -> None:
        if not use_audio():
            return
        spike = audio_det.add_level(seg.end, mean_volume_db(seg.path))
        if spike:
            jobs.put(spike)

    rec = Recorder(stream.url, buffer_dir, keep_seconds=max(240, a.before + a.after + 120),
                   source_file=a.source_file, quality=a.quality, on_segment=on_segment, log=log)

    stop = threading.Event()

    def worker() -> None:
        nonlocal clip_count
        while not stop.is_set():
            try:
                spike = jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            t0, t1 = spike.time - a.before, spike.time + a.after
            # wait until the recording has caught up past the end of the moment
            while rec.latest_time() < t1 and not rec.finished.is_set() and not stop.is_set():
                time.sleep(0.5)
            found = rec.pieces_for(t0, t1)
            if not found:
                log("[clip] the moment isn't in the recording buffer, skipping")
                continue
            paths, offset, duration = found
            if duration < 5:
                log("[clip] too little video for this moment, skipping")
                continue
            stamp = datetime.fromtimestamp(spike.time).strftime("%Y-%m-%d_%H-%M-%S")
            clip_dir = out_root / f"{stamp}_{spike.reason}_{spike.score}x"
            raw_dir = clip_dir / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            # copy the pieces so the rolling buffer can't delete them mid-edit
            copies = []
            for p in paths:
                dst = raw_dir / p.name
                shutil.copy2(p, dst)
                copies.append(dst)
            listing = write_concat_list(copies, raw_dir / "list.txt")
            try:
                out = make_short(listing, offset, duration, clip_dir, opts, log=log)
            except Exception as e:  # noqa: BLE001
                log(f"[clip] editing failed: {e}")
                continue
            if not a.keep_raw:
                shutil.rmtree(raw_dir, ignore_errors=True)
            info = {
                "stream": stream.url, "platform": stream.platform, "detected_at": stamp,
                "reason": spike.reason, "score": spike.score, "seconds": round(duration, 1),
                "suggested_title": _suggest_title(spike.messages), "chat": spike.messages,
            }
            (clip_dir / "info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
            clip_count += 1
            log(f"[clip] ✅ new clip #{clip_count}: {out}")

    log(f"Watching {stream.platform}: {stream.url}")
    log(f"Clips will be saved in: {out_root.resolve()}")
    rec.start()
    if reader:
        reader.start()
    threading.Thread(target=worker, daemon=True).start()

    signal.signal(signal.SIGINT, lambda *_: stop.set())
    warned = False
    started = time.time()
    try:
        while not stop.is_set() and not rec.finished.is_set():
            now = time.time()
            if reader is not None and reader.connected:
                spike = chat_det.tick(now)
                if spike:
                    log(f"[chat] 🔥 chat spike ({spike.score}x normal) — clipping")
                    jobs.put(spike)
            elif reader is not None and not warned and now - started > 60:
                warned = True
                log("[chat] couldn't read chat yet — using loud audio moments to find clips instead")
            stop.wait(1.0)
    finally:
        log("Stopping... (finishing the clip in progress)")
        rec.stop()
        if reader:
            reader.stop()
        # give the worker time to finish what's queued from the recording that exists
        deadline = time.time() + (600 if not stop.is_set() else 120)
        while (not jobs.empty()) and time.time() < deadline:
            time.sleep(1)
        stop.set()
        time.sleep(1)
        shutil.rmtree(buffer_dir, ignore_errors=True)
    log(f"Done. {clip_count} clip(s) in {out_root.resolve()}")
    return 0


# ----------------------------------------------------------------- edit -------
def cmd_edit(a) -> int:
    opts = _edit_options(a)
    src = Path(a.video)
    if a.duration is None:
        from .tools import probe
        total = float(probe(str(src))["format"]["duration"])
        a.duration = max(0.1, total - a.start)
    out_dir = Path(a.out) if a.out else src.with_name(src.stem + "_short")
    out = make_short(src, a.start, a.duration, out_dir, opts, log=log)
    log(f"✅ saved {out}")
    return 0


# ------------------------------------------------------------------- main -----
def _add_edit_args(p) -> None:
    p.add_argument("--layout", choices=["fit", "crop", "facecam"], default="fit",
                   help="fit = whole picture on a blurred background (default); crop = fill the screen; "
                        "facecam = webcam on top, gameplay below (needs --cam)")
    p.add_argument("--cam", help="where the webcam is, as fractions x,y,width,height (e.g. 0.75,0.05,0.22,0.25)")
    p.add_argument("--no-captions", action="store_true", help="don't add captions")
    p.add_argument("--model", default="small", help="speech model: tiny, base, small (default), medium, large-v3")
    p.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="clipwatch", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="watch a live stream and auto-clip the hype moments")
    w.add_argument("url", nargs="?", help="Twitch / YouTube / Kick live link")
    w.add_argument("--out", default="clips", help="folder for finished clips (default: clips)")
    w.add_argument("--before", type=float, default=35, help="seconds kept before the chat spike (default 35)")
    w.add_argument("--after", type=float, default=10, help="seconds kept after the chat spike (default 10)")
    w.add_argument("--sensitivity", type=float, default=3.0,
                   help="how many times busier than normal chat must get (default 3; lower = more clips)")
    w.add_argument("--min-messages", type=int, default=8, help="minimum chat messages in 8 seconds to count")
    w.add_argument("--cooldown", type=float, default=60, help="minimum seconds between clips (default 60)")
    w.add_argument("--audio-jump", type=float, default=9, help="dB louder than usual that counts as a spike")
    w.add_argument("--audio-too", action="store_true", help="also clip loud moments even when chat works")
    w.add_argument("--no-chat", action="store_true", help="don't read chat, only use loud audio moments")
    w.add_argument("--quality", default="best[height<=1080]/best", help="yt-dlp format (default: up to 1080p)")
    w.add_argument("--keep-raw", action="store_true", help="keep the original stream pieces next to each clip")
    w.add_argument("--kick-key", help=argparse.SUPPRESS)
    w.add_argument("--source-file", help=argparse.SUPPRESS)  # testing: treat a local file as the live stream
    w.add_argument("--chat-replay", help=argparse.SUPPRESS)  # testing: replay chat from a .jsonl file
    _add_edit_args(w)
    w.set_defaults(func=cmd_watch)

    e = sub.add_parser("edit", help="turn part of a video file into a vertical short")
    e.add_argument("video")
    e.add_argument("--start", type=float, default=0, help="start time in seconds")
    e.add_argument("--duration", type=float, help="length in seconds (default: to the end)")
    e.add_argument("--out", help="output folder")
    _add_edit_args(e)
    e.set_defaults(func=cmd_edit)

    a = ap.parse_args(argv)
    if a.cmd == "watch" and not a.url and not a.source_file:
        ap.error("give a stream link, e.g. clipwatch watch https://www.twitch.tv/name")
    try:
        sys.exit(a.func(a))
    except (ValueError, RuntimeError) as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
