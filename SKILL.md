---
name: podcast-to-tiktok
description: Use when the user asks to turn a podcast transcript (or podcast URL) into a short cartoon TikTok — e.g. "turn this podcast into a cartoon TikTok", "make an animal-dialogue short from this transcript", "generate a 6-second cartoon video from this podcast". Produces a 6-second 1080x1920 9:16 MP4 with two cartoon animals dialoguing about the podcast's core idea, using MiniMax Hailuo 2.3 for video and OpenAI gpt-audio TTS via an OpenAI-compatible gateway.
metadata: {"requires":"python3.11+, ffmpeg, palebluedot-gateway-key"}
---

# podcast-to-tiktok

Activate when the user asks to:

- "turn this podcast into a cartoon TikTok"
- "make an animal-dialogue short from this transcript"
- "generate a 6-second cartoon video from this podcast"

## Inputs

- **Transcript file** (`.txt`/`.md`) — required. A path the script can read.
- **Hero image URL** — optional. If provided, it is used as the Hailuo first-frame seed. Must be publicly fetchable.

If the user gives a podcast URL, first extract the transcript text into a local file (e.g. `transcript.txt`) and optionally find the `og:image` URL, then invoke the script.

## Invocation

```bash
python skills/podcast-to-tiktok/generate.py \
  --transcript-file <path> \
  [--image <hero-url>] \
  --out out.mp4
```

## Environment

- `OPENAI_API_KEY` — a palebluedot gateway key. Named `OPENAI_API_KEY` because the script uses the OpenAI-compatible SDK convention; the gateway (`https://open.palebluedot.ai/v1`) fronts all three model calls (chat, TTS, video), not OpenAI directly.

Optional overrides:

- `PALEBLUEDOT_BASE_URL` (default `https://open.palebluedot.ai/v1`)
- `CHAT_MODEL` (default `anthropic/claude-opus-4.6`)
- `TTS_MODEL` (default `openai/gpt-audio`)
- `VIDEO_MODEL` (default `MiniMax-Hailuo-2.3`)

## Dependencies

- Python 3.11+, `httpx` (`pip install -r skills/podcast-to-tiktok/requirements.txt`)
- `ffmpeg` on `PATH`

## What the script does

1. Sends the transcript to the chat model and asks for JSON: two animal characters, two voices, two dialogue lines (≤10 words each), and a single-scene video prompt.
2. In parallel: submits the Hailuo 2.3 video task (6 s, 1080P, `first_frame_image = hero URL`) and calls `/audio/speech` twice (one voice per character).
3. Concatenates the two mp3s with a 0.3 s gap via ffmpeg.
4. Polls the Hailuo task every 10 s until it reports `Success`, then downloads the clip.
5. Muxes audio over video, scales/crops to 1080×1920, writes `out.mp4`.

Typical runtime: 1–3 minutes (dominated by Hailuo rendering).
