"""Find the streamer's webcam in a clip, for the split-screen layout.

The webcam can be anywhere: any corner, an edge, the middle, small or full screen.
We grab 16 frames across the clip at full resolution and look for faces everywhere in
each frame with OpenCV's YuNet face detector. Then we decide which face is the streamer:

* The webcam overlay doesn't move, so the streamer's face stays in roughly the same
  place in most frames. Game faces move around and come and go.
* "Roughly" scales with the face: a big face in a full-screen cam can move further than
  a tiny face in a corner cam and still be the same person.
* The streamer's webcam is in the same place clip after clip, so where it was found
  before is remembered per channel and preferred. That stops a game character standing
  still in a cutscene from winning.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import tempfile
from pathlib import Path

from .tools import ffmpeg_exe

MODEL = Path(__file__).with_name("models") / "face_detection_yunet_2023mar.onnx"
FRAMES = 16
MIN_SCORE = 0.5  # detector confidence

Face = tuple[float, float, float, float, float]  # center_x, center_y, width, height (fractions), confidence


def _frames(input_args: list[str], duration: float, outdir: Path) -> list[Path]:
    fps = FRAMES / max(duration, 1.0)
    subprocess.run(
        [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", *input_args,
         "-vf", f"fps={fps:.4f},scale='min(1920,iw)':-2", "-frames:v", str(FRAMES), str(outdir / "f_%02d.png")],
        capture_output=True, check=True,
    )
    return sorted(outdir.glob("f_*.png"))


def find_faces(images: list[Path]) -> list[list[Face]]:
    """Every face in every image, anywhere in the frame."""
    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
    import cv2

    out: list[list[Face]] = []
    det = None
    for p in images:
        img = cv2.imread(str(p))
        if img is None:
            out.append([])
            continue
        h, w = img.shape[:2]
        if det is None:
            det = cv2.FaceDetectorYN.create(str(MODEL), "", (w, h), MIN_SCORE, 0.3, 50)
        det.setInputSize((w, h))
        _, faces = det.detect(img)
        found = []
        for f in faces if faces is not None else []:
            x, y, fw, fh = (float(v) for v in f[:4])
            found.append(((x + fw / 2) / w, (y + fh / 2) / h, fw / w, fh / h, float(f[-1])))
        out.append(found)
    return out


def _same_place(a, b) -> bool:
    """Close enough to be the same webcam face: allowed movement grows with face size."""
    tol_x = max(0.03, 1.2 * max(a[2], b[2]))
    tol_y = max(0.05, 1.2 * max(a[3], b[3]))
    return abs(a[0] - b[0]) < tol_x and abs(a[1] - b[1]) < tol_y


def pick_streamer_face(per_frame: list[list[Face]], min_share: float = 0.3, prefer=None):
    """The face seen in the same place in the most frames (tie: more confident, then
    closer to where this streamer's webcam was before). None if nothing is steady enough."""
    best, best_key = None, None
    n_frames = len(per_frame)
    for faces in per_frame:
        for cand in faces:
            hits = []
            for fs in per_frame:
                near = [f for f in fs if _same_place(f, cand)]
                if near:
                    hits.append(min(near, key=lambda f: abs(f[0] - cand[0]) + abs(f[1] - cand[1])))
            med = tuple(statistics.median(h[i] for h in hits) for i in range(5))
            remembered = 1 if (prefer is not None and _same_place(med, (*prefer, 1.0))
                               and len(hits) >= max(2, min_share * n_frames)) else 0
            # A steady face at the spot where this channel's webcam was before wins outright,
            # so a game character standing still can't take its place.
            key = (remembered, len(hits), statistics.mean(h[4] for h in hits))
            if best_key is None or key > best_key:
                best, best_key = (med, len(hits)), key
    if best is None or n_frames == 0 or best[1] < max(2, min_share * n_frames):
        return None
    return best[0][:4]


def cam_box_from_face(face, frame_aspect: float, panel_aspect: float, zoom: float = 3.6):
    """Crop box (x, y, w, h as fractions) around the face, shaped like the top panel.
    frame_aspect = source width/height, panel_aspect = top panel width/height.
    Works wherever the face is: the box is slid back inside the picture at edges and corners."""
    cx, cy, fw = face[0], face[1], face[2]
    w = min(fw * zoom, 1.0)
    h = w * frame_aspect / panel_aspect  # same shape as the panel, in fractions of the frame
    if h > 1.0:
        h = 1.0
        w = h * panel_aspect / frame_aspect
    x = cx - w / 2
    y = cy - 0.40 * h  # face a bit above the middle
    x = min(max(x, 0.0), 1.0 - w)
    y = min(max(y, 0.0), 1.0 - h)
    return round(x, 4), round(y, 4), round(w, 4), round(h, 4)


def _load_memory(path: Path | None):
    try:
        if path and path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            return d["x"], d["y"], d["w"], d["h"]
    except (ValueError, KeyError, OSError):
        pass
    return None


def _save_memory(path: Path | None, face) -> None:
    if not path:
        return
    try:
        path.write_text(json.dumps({"x": face[0], "y": face[1], "w": face[2], "h": face[3]}), encoding="utf-8")
    except OSError:
        pass


def detect_cam(input_args: list[str], duration: float, frame_aspect: float, panel_aspect: float,
               log=print, memory: Path | None = None):
    """memory: a small file remembering where this channel's webcam face was last time."""
    try:
        with tempfile.TemporaryDirectory(prefix="clipwatch_cam_") as tmp:
            per_frame = find_faces(_frames(input_args, duration, Path(tmp)))
        face = pick_streamer_face(per_frame, prefer=_load_memory(memory))
    except Exception as e:  # noqa: BLE001 - never let this break a clip
        log(f"[edit] couldn't look for the webcam: {e}")
        return None
    if face is None:
        return None
    _save_memory(memory, face)
    return cam_box_from_face(face, frame_aspect, panel_aspect)
