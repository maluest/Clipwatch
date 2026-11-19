"""Record a live stream into short rolling segments on disk.

yt-dlp pulls the stream (Twitch / YouTube / Kick) and pipes it into ffmpeg, which cuts it
into ~4 second .ts pieces without re-encoding. We keep the last few minutes so that when
chat spikes we can go back and grab the moment. Older pieces are deleted.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .tools import ffmpeg_exe

SEGMENT_SECONDS = 4


@dataclass
class Segment:
    path: Path
    start: float  # wall-clock
    end: float


class Recorder:
    def __init__(
        self,
        url: str,
        workdir: Path,
        keep_seconds: float = 300,
        source_file: str | None = None,
        quality: str = "best[height<=1080]/best",
        on_segment: Callable[[Segment], None] | None = None,
        log: Callable[[str], None] = print,
    ):
        self.url = url
        self.workdir = Path(workdir)
        self.keep_seconds = keep_seconds
        self.source_file = source_file
        self.quality = quality
        self.on_segment = on_segment
        self.log = log
        self.segments: list[Segment] = []
        self._lock = threading.Lock()
        self._procs: list[subprocess.Popen] = []
        self._anchor: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.finished = threading.Event()

    # ---- start / stop -------------------------------------------------------------
    def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        for old in self.workdir.glob("seg_*.ts"):
            old.unlink(missing_ok=True)
        csv = self.workdir / "segments.csv"
        csv.unlink(missing_ok=True)

        seg_args = [
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
            "-segment_list", "segments.csv", "-segment_list_type", "csv",
            "-segment_list_flags", "+live", "seg_%06d.ts",
        ]
        ff = ffmpeg_exe()
        if self.source_file:
            # Testing mode: play a local file in real time as if it were a live stream
            cmd = [ff, "-hide_banner", "-loglevel", "error", "-re", "-i", str(Path(self.source_file).resolve()), *seg_args]
            self._procs.append(subprocess.Popen(cmd, cwd=self.workdir, stdin=subprocess.DEVNULL))
        else:
            ytdlp = [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-part",
                     "-f", self.quality, "-o", "-", self.url]
            p1 = subprocess.Popen(ytdlp, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
            cmd = [ff, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", *seg_args]
            p2 = subprocess.Popen(cmd, cwd=self.workdir, stdin=p1.stdout)
            p1.stdout.close()  # let ffmpeg own the pipe
            self._procs += [p1, p2]

        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
        for p in self._procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    # ---- segment bookkeeping -----------------------------------------------------
    def _watch(self) -> None:
        csv = self.workdir / "segments.csv"
        seen = 0
        while not self._stop.is_set():
            if csv.exists():
                lines = csv.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in lines[seen:]:
                    m = re.match(r"^(.+),([\d.]+),([\d.]+)$", line.strip())
                    if not m:
                        continue
                    name, s, e = m.group(1), float(m.group(2)), float(m.group(3))
                    if self._anchor is None:
                        self._anchor = time.time() - e
                        self.log("[record] stream is recording")
                    seg = Segment(self.workdir / name, self._anchor + s, self._anchor + e)
                    with self._lock:
                        self.segments.append(seg)
                    if self.on_segment:
                        try:
                            self.on_segment(seg)
                        except Exception as ex:  # noqa: BLE001
                            self.log(f"[record] segment hook failed: {ex}")
                seen = len(lines)
            self._cleanup()
            if all(p.poll() is not None for p in self._procs):
                code = self._procs[-1].returncode
                self.log(f"[record] stream recording stopped (ffmpeg exit code {code}). "
                         "The stream may have ended, be offline, or need a login.")
                self.finished.set()
                return
            self._stop.wait(0.5)
        self.finished.set()

    def _cleanup(self) -> None:
        cutoff = time.time() - self.keep_seconds
        with self._lock:
            while self.segments and self.segments[0].end < cutoff:
                seg = self.segments.pop(0)
                try:
                    seg.path.unlink(missing_ok=True)
                except OSError:
                    pass  # Windows may still have it open; next cleanup will get it

    def latest_time(self) -> float:
        with self._lock:
            return self.segments[-1].end if self.segments else 0.0

    def pieces_for(self, t0: float, t1: float) -> tuple[list[Path], float, float] | None:
        """Segments covering [t0, t1], plus where the clip starts inside them and how long it is."""
        with self._lock:
            chosen = [s for s in self.segments if s.end > t0 and s.start < t1]
        if not chosen:
            return None
        start = max(t0, chosen[0].start)
        end = min(t1, chosen[-1].end)
        return [s.path for s in chosen], start - chosen[0].start, end - start


def write_concat_list(paths: list[Path], dest: Path) -> Path:
    with open(dest, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '" + str(Path(p).resolve()).replace("\\", "/").replace("'", "'\\''") + "'\n")
    return dest


def mean_volume_db(path: Path) -> float:
    out = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", os.devnull],
        capture_output=True, text=True, errors="replace",
    ).stderr
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", out)
    return float(m.group(1)) if m else -91.0
