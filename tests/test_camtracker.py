from clipwatch.camtracker import GONE_AFTER, LOCK_MIN, MOVED_AFTER, CamTracker

STREAMER = (0.84, 0.53, 0.06, 0.13, 0.95)  # like the green-screen streamer on the right


def tracker(tmp_path=None):
    return CamTracker(recorder=None, panel_aspect=1080 / 760,
                      memory=(tmp_path / "webcam_frame.json") if tmp_path else None, log=lambda m: None)


def game_face(i):
    return (0.1 + (i % 7) * 0.1, 0.4 + (i % 3) * 0.15, 0.05, 0.1, 0.9)


def lock(t, face=STREAMER, n=LOCK_MIN):
    for i in range(n):
        t.observe([face, game_face(i)])


def test_locks_on_streamer_not_moving_game_faces():
    t = tracker()
    for i in range(LOCK_MIN - 1):
        t.observe([STREAMER, game_face(i)])
    assert t.current_box() is None  # not enough watching yet
    t.observe([STREAMER, game_face(9)])
    x, y, w, h = t.current_box()
    assert x < 0.84 < x + w and y < 0.53 < y + h


def test_frame_stays_put_when_streamer_looks_away_or_stands_up():
    t = tracker()
    lock(t)
    box = t.current_box()
    for _ in range(MOVED_AFTER - 1):  # looking down / out of frame for ~1.5 min
        t.observe([])
    t.observe([(0.84, 0.25, 0.06, 0.13, 0.9)])  # stood up, head higher in the same cam
    assert t.current_box() == box


def test_relocks_when_webcam_moves():
    t = tracker()
    lock(t)
    new_spot = (0.12, 0.2, 0.05, 0.1, 0.9)
    for _ in range(MOVED_AFTER):
        t.observe([new_spot])
    x, y, w, h = t.current_box()
    assert x < 0.12 < x + w and y < 0.2 < y + h


def test_unlocks_when_webcam_gone():
    t = tracker()
    lock(t)
    for _ in range(GONE_AFTER):
        t.observe([])
    assert t.current_box() is None


def test_game_face_standing_still_for_a_minute_does_not_get_locked():
    t = tracker()
    still_game = (0.45, 0.6, 0.07, 0.15, 0.97)
    for i in range(LOCK_MIN + 3):
        faces = [STREAMER]
        if i < 3:  # cutscene: game character still for ~1 minute
            faces.append(still_game)
        t.observe(faces)
    x, y, w, h = t.current_box()
    assert x < 0.84 < x + w


def test_green_screen_streamer_moving_around_is_all_inside_the_frame():
    t = tracker()
    spots = [(0.80, 0.50), (0.88, 0.55), (0.84, 0.45), (0.82, 0.58), (0.86, 0.52), (0.84, 0.50)]
    for cx, cy in spots:
        t.observe([(cx, cy, 0.06, 0.13, 0.9)])
    x, y, w, h = t.current_box()
    for cx, cy in spots:
        assert x < cx < x + w and y < cy < y + h


def test_frame_is_remembered_for_next_time(tmp_path):
    t = tracker(tmp_path)
    lock(t)
    assert tracker(tmp_path).current_box() == t.current_box()


def _frame_with_webcam(cam_bottom=0.40, banner=False, noise_seed=0):
    import numpy as np
    rng = np.random.default_rng(noise_seed)
    H, W = 540, 960
    g = rng.integers(60, 200, size=(H, W)).astype(np.uint8)  # busy "game"
    y1 = int(cam_bottom * H)
    ch = y1 - 40
    cam = np.full((ch, 260), 120, np.uint8)
    cam[40:110, 90:170] = 70  # a "head"
    cam[110:, 40:220] = rng.integers(40, 90, size=(ch - 110, 180))  # a "body" with uneven texture
    g[40:y1, 680:940] = cam
    if banner:  # green-screen style: a bright banner right under the body
        g[y1:y1 + 30, 660:950] = 240
    return g


def test_finds_webcam_bottom_edge():
    from clipwatch.facecam import find_bottom_cut
    face = (0.84, 0.20, 0.06, 0.12)
    grays = [_frame_with_webcam(0.40, noise_seed=s) for s in range(5)]
    cut = find_bottom_cut(grays, face)
    assert cut is not None and abs(cut - 0.40) < 0.02


def test_cut_is_top_of_banner_not_bottom():
    from clipwatch.facecam import find_bottom_cut
    face = (0.84, 0.20, 0.06, 0.12)
    grays = [_frame_with_webcam(0.40, banner=True, noise_seed=s) for s in range(5)]
    cut = find_bottom_cut(grays, face)
    assert abs(cut - 0.40) < 0.02  # banner bottom is at ~0.455


def test_no_edge_means_no_cut():
    import numpy as np
    from clipwatch.facecam import find_bottom_cut
    rng = np.random.default_rng(1)
    grays = [rng.integers(100, 110, size=(540, 960)).astype(np.uint8) for _ in range(4)]
    assert find_bottom_cut(grays, (0.5, 0.3, 0.06, 0.12)) is None


def test_box_bottom_sits_on_cut_and_keeps_head():
    from clipwatch.facecam import fit_box_to_cut
    face = (0.88, 0.28, 0.04, 0.09)
    x, y, w, h = fit_box_to_cut((0.80, 0.20, 0.15, 0.19), face, 0.385, 16 / 9, 1080 / 760)
    assert abs((y + h) - 0.385) < 0.002
    assert y <= 0.28 - 1.4 * 0.09 + 1e-6  # head fits
    assert 0 <= x and x + w <= 1.0001
