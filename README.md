# clipwatch

Watches a **live stream** on Twitch, YouTube or Kick. When **chat suddenly goes crazy**, it cuts that moment and turns it into a **ready-to-post vertical short** (1080×1920) with captions and levelled sound. You don't have to wait for the stream to end or watch hours of video.

```
stream ──► record the last few minutes ──► read live chat
                                              │
                         chat 3× busier than normal? ("KEKW", "NO WAY", "clip it"...)
                                              │
              cut 35s before → 10s after ──► vertical layout + captions + loudness ──► clips\
```

If it can't read chat (for example, Kick changed something), it falls back to **loud moments** in the audio instead, like the streamer shouting or explosions.

> **Only clip streamers who allow it**, and credit them. Many say so in their channel description or panels.

---

## Setup on Windows (once)

1. Download this project: on GitHub, click the green **Code** button, then **Download ZIP**, and unzip it somewhere such as `Documents\clipwatch`.
2. Double-click **`install.bat`**. It installs Python, FFmpeg and Deno (needed for YouTube) if you don't have them, then everything else.
   - If it says programs were just installed, close the window and double-click `install.bat` again.
   - At the end it asks for your **Anthropic API key** (from [console.anthropic.com](https://console.anthropic.com), pay-as-you-go, separate from a Claude subscription). Claude uses it to write each clip's hook title, post caption and hashtags. Press Enter to skip, and clips are made without hooks. The key is saved in `anthropic_key.txt` in this folder; delete that file to change it.
   - Got a newer version of clipwatch? Run `install.bat` again to update.

## Use it

Double-click **`clip.bat`**, paste the live stream link, and press Enter. Leave it running while the stream is live. Press **Ctrl+C** to stop.

The link must be a stream that is **live right now**. Normal videos and finished streams won't work; use `edit` for those (see below).

Links that work:
- `https://www.twitch.tv/streamername`
- `https://kick.com/streamername`
- `https://www.youtube.com/@channelname/live`, or a `youtube.com/watch?v=...` link of the live stream

Twitch shows "Commercial break in progress" ads to viewers who aren't logged in. clipwatch records Twitch with Streamlink, which skips those ads, so the first minute or two may show nothing new while an ad plays. If the video ever stops arriving, it reconnects by itself.

Each clip gets its own folder in `clips\<channel>\`:
| File | What |
|---|---|
| `clip.mp4` | the finished vertical short |
| `info.json` | why it was clipped, the chat messages at that moment, and a title idea |
| `post.txt` | **ready to paste**: the post caption and hashtags Claude wrote |
| `hook.json` | the hook title, caption, hashtags and a one-line summary of what happens |
| `transcript.txt` | what was said, with timings |
| `captions.ass` | the caption file, if you want to edit the wording |

The first clip takes longer because the speech-to-text model (about 0.5 GB) downloads once.

## Hook titles (Claude)

For every clip, Claude (Opus 5.5) looks at 6 frames from across the clip, the transcript and how chat reacted, works out what actually happened, and writes:
- a **hook title**, shown big for the **first 3 seconds** (no captions under it), with the key word in yellow,
- a **post caption** and **hashtags** (`post.txt`),
- a one-line **summary** (`hook.json`).

It costs roughly 5–7 cents per clip on your Anthropic account. If there's no key, no internet, or Claude declines, the clip is still made, just without a hook.

## Options

From a terminal in the project folder (`.venv\Scripts\python -m clipwatch watch LINK [options]`), or added after `clip.bat`:

| Option | What it does |
|---|---|
| `--sensitivity 2.5` | lower = more clips (default 3 = chat 3× busier than normal) |
| `--before 35 --after 10` | seconds kept before / after the chat spike |
| `--cooldown 60` | minimum seconds between clips |
| *(default)* | **split screen**: finds the streamer's webcam wherever it is (any corner, edge, the middle, or full screen) and puts it on top, the game below, captions on the seam. If no webcam is found, it uses `fit`. |
| `--cam 0.75,0.05,0.22,0.25` | set the top crop yourself if the automatic one is off: left, top, width, height as fractions of the stream picture. Each clip folder has a `cam.txt` with the crop it used, which is a good starting point. |
| `--layout fit` | whole picture on a blurred background |
| `--layout crop` | zoom in to fill the screen (cuts off the sides) |
| `--no-captions` | skip captions (faster) |
| `--model small` | speech model. Default `auto`: `large-v3-turbo` on an NVIDIA card (most accurate words and timing), `small` without one |
| `--no-hook` | don't ask Claude for a hook / post caption |
| `--hook-effort high` | Claude thinks harder about each hook (better, costs more; default `medium`) |
| `--audio-too` | also clip very loud moments, even when chat is working |
| `--quality "best[height<=720]/best"` | lower quality if your internet is slow |

### Make a short from any video file
```
.venv\Scripts\python -m clipwatch edit "my video.mp4" --start 120 --duration 40 --layout fit
```

## Good to know
- **Webcam frame:** while it records, clipwatch checks the picture every 20 seconds. Once the streamer has been in the same spot for about 2 minutes, it **locks** a fixed frame there and uses it for every clip. Standing up, looking away or a game character on screen doesn't change it. It keeps checking: if the webcam moves to another part of the screen, it locks a new frame there; if the webcam disappears for ~5 minutes, clips use the full picture until it's back. The frame is saved in `clips\<channel>\webcam_frame.json`, so the next time you watch that streamer it's ready from the first clip. (Before the first lock, each clip looks for the webcam by itself.)
- Your computer has to stay on (and not sleep) while it watches.
- It keeps only the last few minutes of stream on disk, so it doesn't fill your drive.
- With an NVIDIA graphics card, captions use the large, more accurate speech model and are fast (`install.bat` installs the NVIDIA libraries it needs). Without one, a smaller model runs on the CPU. The first clip downloads the model (~1.6 GB for the NVIDIA one).
- Twitch chat is read anonymously, so no login is needed. YouTube chat is read with yt-dlp, which `clip.bat` updates automatically each time it starts. Kick chat uses Kick's website chat connection, which Kick could change at any time; audio detection takes over if it breaks.
