"""Find the streamer's webcam in a clip, for the split-screen layout.

We grab ~12 frames across the clip and look for faces with OpenCV's YuNet detector.
The streamer's face stays in the same spot (the webcam overlay doesn't move), while
faces in the game move around, so we pick the face that shows up in the same place in
the most frames, and crop a box around it.
"""
from __future__ import annotations

import statistics
import subprocess
import tempfile
from pathlib import Path

from .tools import ffmpeg_exe

MODEL = Path(__file__).with_name("models") / "face_detection_yunet_2023mar.onnx"
FRAMES = 12


def _frames(input_args: list[str], duration: float, outdir: Path) -> list[Path]:
    fps = FRAMES / max(duration, 1.0)
    subprocess.run(
        [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", *input_args,
         "-vf", f"fps={fps:.4f},scale=1280:-2", "-frames:v", str(FRAMES), str(outdir / "f_%02d.png")],
        capture_output=True, check=True,
    )
    return sorted(outdir.glob("f_*.png"))


def find_faces(images: list[Path]) -> list[list[tuple[float, float, float, float]]]:
    """Per image: faces as (center_x, center_y, width, height), fractions of the frame."""
    import os

    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
    import cv2

    out = []
    det = None
    for p in images:
        img = cv2.imread(str(p))
        if img is None:
            out.append([])
            continue
        h, w = img.shape[:2]
        if det is None:
            det = cv2.FaceDetectorYN.create(str(MODEL), "", (w, h), 0.6)
        det.setInputSize((w, h))
        _, faces = det.detect(img)
        found = []
        for f in faces if faces is not None else []:
            x, y, fw, fh = (float(v) for v in f[:4])
            found.append(((x + fw / 2) / w, (y + fh / 2) / h, fw / w, fh / h))
        out.append(found)
    return out


def pick_streamer_face(per_frame, min_share: float = 0.4, near: float = 0.05):
    """The face seen in the same place in the most frames. None if nothing is steady enough."""
    best, best_hits = None, 0
    for faces in per_frame:
        for cand in faces:
            hits = [min(fs, key=lambda f: abs(f[0] - cand[0]) + abs(f[1] - cand[1]))
                    for fs in per_frame
                    if fs and any(abs(f[0] - cand[0]) < near and abs(f[1] - cand[1]) < near for f in fs)]
            if len(hits) > best_hits:
                best_hits = len(hits)
                best = tuple(statistics.median(h[i] for h in hits) for i in range(4))
    frames_with_data = len(per_frame)
    if best is None or frames_with_data == 0 or best_hits < max(2, min_share * frames_with_data):
        return None
    return best


def cam_box_from_face(face, frame_aspect: float, panel_aspect: float, zoom: float = 3.6):
    """Crop box (x, y, w, h as fractions) around the face, shaped like the top panel.
    frame_aspect = source width/height, panel_aspect = top panel width/height."""
    cx, cy, fw, _fh = face
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


def detect_cam(input_args: list[str], duration: float, frame_aspect: float, panel_aspect: float, log=print):
    try:
        with tempfile.TemporaryDirectory(prefix="clipwatch_cam_") as tmp:
            face = pick_streamer_face(find_faces(_frames(input_args, duration, Path(tmp))))
    except Exception as e:  # noqa: BLE001 - never let this break a clip
        log(f"[edit] couldn't look for the webcam: {e}")
        return None
    if face is None:
        return None
    return cam_box_from_face(face, frame_aspect, panel_aspect)
