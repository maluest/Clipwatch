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
