"""Lock the streamer's webcam frame once per stream, and re-check it from time to time.

While the stream records, one frame is checked every 20 seconds. Once the streamer's face
has been in the same place for most of the last ~2 minutes, that spot is LOCKED and a
fixed frame (covering everywhere the face has been, plus head and shoulders) is used for
every clip. Standing up, looking down or stepping away doesn't change it.

It keeps checking. If the face hasn't been at the locked spot for ~2 minutes and is
steady somewhere else, the webcam was moved: a new frame is locked there. If the face
is gone everywhere for ~5 minutes (BRB screen, full-screen game), the lock is dropped and
clips use the full-picture layout until the webcam is back.

Game faces don't stay in one spot for minutes, so they can't get locked.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from collections import deque
from pathlib import Path

from .facecam import _same_place, box_from_hits, find_faces, median_face, steady_hits
from .tools import ffmpeg_exe

INTERVAL = 20  # seconds between checks
WINDOW = 9  # checks looked at when locking (~3 minutes)
LOCK_MIN = 6  # checks needed before the first lock (~2 minutes)
MOVED_AFTER = 6  # checks in a row without the face at the locked spot -> look for a new spot
GONE_AFTER = 15  # checks in a row without it -> drop the lock


class CamTracker(threading.Thread):
    def __init__(self, recorder, panel_aspect: float, memory: Path | None = None, log=print,
                 interval: float = INTERVAL):
        super().__init__(daemon=True)
        self.rec = recorder
        self.panel_aspect = panel_aspect
        self.memory = memory
        self.log = log
        self.interval = interval
        self.obs: deque[list] = deque(maxlen=WINDOW)
        self.frame_aspect: float | None = None
        self.face = None  # locked face position (cx, cy, w, h)
        self.box = None  # locked crop (x, y, w, h)
        self.misses = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._load()

    # ---- public ----------------------------------------------------------------
    def current_box(self):
        with self._lock:
            return self.box

    def stop(self) -> None:
        self._stop.set()

    # ---- memory ----------------------------------------------------------------
    def _load(self) -> None:
        try:
            if self.memory and self.memory.exists():
                d = json.loads(self.memory.read_text(encoding="utf-8"))
                if "box" in d:
                    self.face = (d["x"], d["y"], d["w"], d["h"])
                    self.box = tuple(d["box"])
                    self.log(f"[cam] using this streamer's saved webcam frame {self._fmt(self.box)} (re-checking it as we go)")
        except (ValueError, KeyError, OSError, TypeError):
            pass

    def _save(self) -> None:
        if not self.memory:
            return
        try:
            x, y, w, h = self.face
            self.memory.write_text(json.dumps({"x": x, "y": y, "w": w, "h": h, "box": list(self.box)}), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _fmt(box) -> str:
        return ",".join(f"{v:.3f}" for v in box)

    # ---- checking ----------------------------------------------------------------
    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                faces = self._look()
            except Exception as e:  # noqa: BLE001 - never stop the stream over this
                self.log(f"[cam] check failed: {e}")
                continue
            if faces is not None:
                self.observe(faces)

    def _look(self):
        seg = self.rec.latest_segment()
        if seg is None or not seg.exists():
            return None
        with tempfile.TemporaryDirectory(prefix="clipwatch_tr_") as tmp:
            png = Path(tmp) / "f.png"
            subprocess.run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(seg),
                            "-frames:v", "1", "-vf", "scale='min(1920,iw)':-2", str(png)],
                           capture_output=True)
            if not png.exists():
                return None
            if self.frame_aspect is None:
                os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
                import cv2
                img = cv2.imread(str(png))
                self.frame_aspect = img.shape[1] / img.shape[0]
            return find_faces([png])[0]

    def observe(self, faces) -> None:
        """Feed one check's faces (list of (cx, cy, w, h, conf)). Updates the lock."""
        self.obs.append(faces)
        aspect = self.frame_aspect or 16 / 9
        with self._lock:
            if self.face is not None:
                if any(_same_place(f, (*self.face, 1.0)) for f in faces):
                    self.misses = 0
                    return
                self.misses += 1
                if self.misses >= MOVED_AFTER:
                    recent = list(self.obs)[-MOVED_AFTER:]
                    hits = steady_hits(recent, min_share=0.6)
                    if hits:
                        self._set(hits, aspect, "the webcam moved: new frame locked at")
                        return
                if self.misses >= GONE_AFTER:
                    self.log("[cam] no webcam for a few minutes: using the full picture until it's back")
                    self.face = self.box = None
                    self.misses = 0
                return
            if len(self.obs) >= LOCK_MIN:
                hits = steady_hits(list(self.obs), min_share=0.6)
                if hits:
                    self._set(hits, aspect, "webcam locked at")

    def _set(self, hits, aspect, msg) -> None:
        self.face = median_face(hits)
        self.box = box_from_hits(hits, aspect, self.panel_aspect)
        self.misses = 0
        self.log(f"[cam] {msg} {self._fmt(self.box)}")
        self._save()
