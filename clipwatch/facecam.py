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


def steady_hits(per_frame: list[list[Face]], min_share: float = 0.3, prefer=None):
    """The detections (one per frame) of the face seen in the same place in the most frames.
    Ties: more confident. A steady face at the remembered spot (prefer) wins outright.
    Returns None if no face is steady enough."""
    best, best_key = None, None
    n_frames = len(per_frame)
    need = max(2, min_share * n_frames)
    for faces in per_frame:
        for cand in faces:
            hits = []
            for fs in per_frame:
                near = [f for f in fs if _same_place(f, cand)]
                if near:
                    hits.append(min(near, key=lambda f: abs(f[0] - cand[0]) + abs(f[1] - cand[1])))
            med = tuple(statistics.median(h[i] for h in hits) for i in range(5))
            remembered = 1 if (prefer is not None and _same_place(med, (*prefer[:4], 1.0)) and len(hits) >= need) else 0
            key = (remembered, len(hits), statistics.mean(h[4] for h in hits))
            if best_key is None or key > best_key:
                best, best_key = hits, key
    if best is None or n_frames == 0 or len(best) < need:
        return None
    return best


def median_face(hits):
    return tuple(statistics.median(h[i] for h in hits) for i in range(4))


def pick_streamer_face(per_frame: list[list[Face]], min_share: float = 0.3, prefer=None):
    """Middle position of the steadiest face (see steady_hits), or None."""
    hits = steady_hits(per_frame, min_share, prefer)
    return median_face(hits) if hits else None


def box_from_hits(hits, frame_aspect: float, panel_aspect: float, zoom: float = 3.6):
    """A fixed frame covering everywhere the streamer's face has been, with room for
    head and shoulders, shaped like the top panel. Used for the locked webcam frame."""
    cx = [h[0] for h in hits]
    cy = [h[1] for h in hits]
    fw = statistics.median(h[2] for h in hits)
    fh = statistics.median(h[3] for h in hits)
    spread_x = max(cx) - min(cx)
    spread_y = max(cy) - min(cy)
    w = max(fw * zoom, spread_x + fw * 2.5)
    h = w * frame_aspect / panel_aspect
    if h < spread_y + fh * 2.5:  # moved up/down a lot: make it taller (and wider to keep the shape)
        h = spread_y + fh * 2.5
        w = h * panel_aspect / frame_aspect
    if w > 1.0:
        w = 1.0
        h = w * frame_aspect / panel_aspect
    if h > 1.0:
        h = 1.0
        w = h * panel_aspect / frame_aspect
    mid_x = (max(cx) + min(cx)) / 2
    x = mid_x - w / 2
    y = (min(cy) + max(cy)) / 2 - 0.40 * h
    x = min(max(x, 0.0), 1.0 - w)
    y = min(max(y, 0.0), 1.0 - h)
    return round(x, 4), round(y, 4), round(w, 4), round(h, 4)


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


def load_gray(path: Path, width: int = 960):
    """A frame as a small greyscale array (for finding the webcam's bottom edge)."""
    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
    import cv2
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    h, w = img.shape[:2]
    return cv2.resize(img, (width, max(2, round(h * width / w))), interpolation=cv2.INTER_AREA)


def find_bottom_cut(grays, face, min_score: float = 0.85):
    """Where the webcam (or a green-screen cutout) ends below the streamer, as a fraction of
    the frame height, or None.

    The bottom of a webcam overlay, or the line where a cutout body meets a banner, is a
    straight, sharp horizontal edge across the whole width under the face, in the same row
    in every frame. Body outlines, mics and chairs aren't straight across. We take the
    FIRST such edge below the face (the top of a banner, not its bottom)."""
    import numpy as np

    grays = [g for g in grays if g is not None]
    if not grays:
        return None
    H, W = grays[0].shape
    cx, cy, fw, fh = face[:4]
    x0 = int(max(0, (cx - 1.3 * fw) * W))
    x1 = int(min(W, (cx + 1.3 * fw) * W))
    y0 = int((cy + 1.0 * fh) * H)
    y1 = int(min(H - 2, (cy + 7 * fh) * H))
    if x1 - x0 < 8 or y1 <= y0:
        return None
    per_frame = []
    for g in grays:
        if g.shape != (H, W):
            continue
        a = g[:, x0:x1].astype(np.float32)
        d = np.abs(a[2:] - a[:-2])  # value for row y is at index y-1
        per_frame.append((d > 18).mean(axis=1))
    if not per_frame:
        return None
    score = np.median(np.stack(per_frame), axis=0)  # must be there in most frames
    for y in range(y0, y1):
        if score[y - 1] >= min_score:
            band = range(y, min(y + 6, y1))  # an edge is a few rows thick: take its sharpest row
            best = max(band, key=lambda r: score[r - 1])
            return max(0.0, (best - 3) / H)  # stop just above it, so no border line shows at the seam
    return None


def fit_box_to_cut(box, face, cut_y: float, frame_aspect: float, panel_aspect: float):
    """Move the crop so its bottom sits exactly on the webcam's bottom edge, keeping the
    panel's shape, and keeping the whole head in the picture."""
    x, y, w, h = box
    cx, cy, fw, fh = face[:4]
    head_top = max(0.0, cy - 1.4 * fh)
    if cut_y - h > head_top:  # box would start below the top of the head: make it taller (and wider)
        h = cut_y - head_top
    h = min(h, cut_y)
    w = min(1.0, h * panel_aspect / frame_aspect)
    h = w * frame_aspect / panel_aspect
    y = max(0.0, cut_y - h)
    x = min(max(cx - w / 2, 0.0), 1.0 - w)
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
            images = _frames(input_args, duration, Path(tmp))
            per_frame = find_faces(images)
            grays = [load_gray(p) for p in images]
        face = pick_streamer_face(per_frame, prefer=_load_memory(memory))
        if face is None:
            return None
        _save_memory(memory, face)
        box = cam_box_from_face(face, frame_aspect, panel_aspect)
        cut = find_bottom_cut(grays, face)
        if cut is not None:
            box = fit_box_to_cut(box, face, cut, frame_aspect, panel_aspect)
            log(f"[edit] webcam bottom edge found at {cut:.3f} of the height: top panel ends there")
        return box
    except Exception as e:  # noqa: BLE001 - never let this break a clip
        log(f"[edit] couldn't look for the webcam: {e}")
        return None
