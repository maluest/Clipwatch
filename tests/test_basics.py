import pytest

from clipwatch.captions import Word, build_ass, group_words
from clipwatch.chat.twitch import parse_privmsg
from clipwatch.detector import AudioSpikeDetector, ChatSpikeDetector
from clipwatch.editor import parse_cam
from clipwatch.platforms import parse_stream_url


@pytest.mark.parametrize("url,platform,channel", [
    ("https://www.twitch.tv/SomeStreamer", "twitch", "somestreamer"),
    ("twitch.tv/abc_123?sr=a", "twitch", "abc_123"),
    ("https://kick.com/xQc", "kick", "xqc"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5", "youtube", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/live/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/@SomeChannel/live", "youtube", "SomeChannel"),
])
def test_urls(url, platform, channel):
    s = parse_stream_url(url)
    assert (s.platform, s.channel) == (platform, channel)


def test_bad_url():
    with pytest.raises(ValueError):
        parse_stream_url("https://example.com/video")


def test_twitch_privmsg():
    line = "@badge-info=;color=#FF0000 :bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :KEKW no way"
    assert parse_privmsg(line) == "KEKW no way"
    assert parse_privmsg(":tmi.twitch.tv 001 justinfan1 :Welcome") is None


def _feed(det, t0, t1, gap, text="hi"):
    t = t0
    while t < t1:
        det.add_message(t, text)
        t += gap


def test_chat_spike_fires_once_and_not_on_normal_chat():
    det = ChatSpikeDetector(warmup=30, cooldown=60)
    _feed(det, 0, 120, 1.0)  # normal: 1 msg/s
    fired = [det.tick(float(s)) for s in range(1, 121)]
    assert not any(fired)
    _feed(det, 120, 130, 0.1, "KEKW")  # burst: 10 msg/s
    fired = [f for f in (det.tick(float(s)) for s in range(121, 131)) if f]
    assert len(fired) == 1  # cooldown stops repeats
    assert fired[0].score >= 3


def test_quiet_chat_needs_min_messages():
    det = ChatSpikeDetector(warmup=0, min_messages=8)
    det.add_message(1.0, "hi")
    det.add_message(1.5, "hi")
    assert det.tick(2.0) is None


def test_audio_spike():
    det = AudioSpikeDetector(jump_db=9, warmup=5)
    for i in range(10):
        assert det.add_level(i * 4.0, -30.0) is None
    assert det.add_level(44.0, -18.0) is not None


def test_captions_grouping_and_ass():
    words = [Word(i * 0.3, i * 0.3 + 0.25, w) for i, w in enumerate("no way he did that. what".split())]
    groups = group_words(words)
    assert [len(g) for g in groups] == [3, 2, 1]
    ass = build_ass(words)
    assert ass.count("Dialogue:") == 6
    assert r"{\c&H00FFFF&}NO{\c&HFFFFFF&} WAY HE" in ass


def test_parse_cam():
    assert parse_cam("0.7,0.05,0.25,0.3") == (0.7, 0.05, 0.25, 0.3)
    with pytest.raises(ValueError):
        parse_cam("0.9,0,0.5,0.5")


def test_youtube_chat_line():
    import json
    from clipwatch.chat.youtube import message_text
    line = json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {
        "liveChatTextMessageRenderer": {"message": {"runs": [
            {"text": "NO WAY "}, {"emoji": {"emojiId": "x", "shortcuts": [":skull:"]}}]}}}}}]},
        "videoOffsetTimeMsec": "1000", "isLive": True})
    assert message_text(line) == "NO WAY :skull:"
    assert message_text('{"replayChatItemAction": {"actions": [{"addLiveChatTickerItemAction": {}}]}}') is None
    assert message_text("not json") is None


def test_streamer_face_is_the_one_that_stays_put():
    from clipwatch.facecam import cam_box_from_face, pick_streamer_face
    cam = (0.86, 0.27, 0.04, 0.09)
    frames = [[cam, (0.1 + i * 0.07, 0.6, 0.05, 0.1)] for i in range(10)]  # game face moves around
    face = pick_streamer_face(frames)
    assert abs(face[0] - 0.86) < 0.01 and abs(face[1] - 0.27) < 0.01
    x, y, w, h = cam_box_from_face(face, 16 / 9, 1080 / 760)
    assert 0 <= x and x + w <= 1.0001 and 0 <= y and y + h <= 1.0001
    assert x < 0.86 < x + w and y < 0.27 < y + h


def test_no_steady_face_means_no_split_screen():
    from clipwatch.facecam import pick_streamer_face
    frames = [[(0.1 + i * 0.08, 0.5, 0.05, 0.1)] for i in range(10)]
    assert pick_streamer_face(frames) is None
    assert pick_streamer_face([[] for _ in range(10)]) is None


def test_captions_on_the_seam():
    words = [Word(0, 0.3, "hi")]
    ass = build_ass(words, center_y=760)
    assert r"{\pos(540,760)}" in ass and ",7,3,5,60,60," in ass
