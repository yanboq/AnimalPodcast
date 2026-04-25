# podcast-to-tiktok

Turns a podcast transcript into a vertical 9:16 TikTok video. Default is a single 15-second hot-take: one cartoon animal delivers the punchiest takeaway to a silent second animal in frame. Multi-beat dialogues (`--num-beats 2/4/...`) chain visually so the cast stays consistent across cuts.

- **Visuals + audio (one model):** Seedance 2.0 Fast renders 720×1280 9:16 cartoon clips with native lip-synced dialogue inside each clip.
- **Dialogue:** Claude reads the transcript, picks the most concrete takeaway(s), and writes the line(s) — also picks the two animals and a `voice_description` per character that drives Seedance's voice.
- **Pipeline:** chat → N sequential Seedance tasks (chained via last-frame seeding) → ffmpeg concat. No separate TTS pass; no audio mux.
- **Gateways:** every API call hits an OpenAI-compatible base URL. Default is **TokenRouter** (`https://api.tokenrouter.com/v1`), but chat and video can target different gateways via per-layer overrides.

## Install

### In OpenClaw

```bash
openclaw install https://github.com/yanboq/AnimalPodcast
```

### Manually

```bash
git clone https://github.com/yanboq/AnimalPodcast
cd AnimalPodcast
pip install -r skills/podcast-to-tiktok/requirements.txt
```

## Prerequisites

- Python 3.9+
- `ffmpeg` on `PATH` (macOS: `brew install ffmpeg`)
- A **TokenRouter API token** — see Setup below

## Setup

The first-run wizard captures your gateway key and default model choices, then writes them to `~/.config/podcast-to-tiktok/config.json` (mode `0600`).

```bash
python skills/podcast-to-tiktok/generate.py setup
```

What it asks:

1. Single provider for everything? (Y/n) — defaults to yes.
2. Pick a provider (TokenRouter / OpenAI direct / OpenRouter / custom).
3. API key (entered with `getpass`, no echo).
4. Probe the gateway? (optional sanity check via `GET /models`).
5. Pick a chat model from a curated list (or `custom...`).
6. Pick a video model from a curated list (or `custom...`).
7. Default video length and number of beats.

Re-running `setup` later lets you keep, edit, or rewrite the saved config — useful for rotating keys or trying a different model.

### Why TokenRouter?

TokenRouter exposes Claude (chat), Seedance 2.0 Fast (video), and other models behind a single OpenAI wire-format endpoint with a single key. The script speaks that wire format, so any OpenAI-compatible gateway works for the chat layer; the video layer needs a gateway that exposes `/video/generations` (TokenRouter does; OpenAI direct and OpenRouter do not).

> The env var is named `OPENAI_API_KEY` because the wire format is OpenAI's — but the value must be a **TokenRouter** key (or whatever gateway you've configured). An `openai.com` key will not work for the video layer.

## Usage

```bash
python skills/podcast-to-tiktok/generate.py generate \
  --transcript-file episode.txt \
  --image https://example.com/hero.jpg \
  --length 15 \
  --num-beats 1 \
  --out out.mp4
```

The bare form still works for backwards compatibility:

```bash
python skills/podcast-to-tiktok/generate.py --transcript-file episode.txt --out out.mp4
```

### Examples

```bash
SKILL=skills/podcast-to-tiktok/generate.py

# Default: 15-second TikTok hot take (1 beat × 15s)
python $SKILL generate --transcript-file episode.txt --out clip.mp4

# 30-second clip with one back-and-forth (2 beats × 15s, chained for character continuity)
python $SKILL generate \
  --transcript-file episode.txt \
  --length 30 --num-beats 2 \
  --out clip.mp4

# 60-second clip with two back-and-forths (4 beats × 15s, sequential chain)
python $SKILL generate \
  --transcript-file episode.txt \
  --length 60 --num-beats 4 \
  --out clip.mp4

# Faster / cheaper chat model (sonnet); video stays on the saved default
python $SKILL generate \
  --transcript-file episode.txt \
  --chat-model anthropic/claude-sonnet-4.6 \
  --out clip.mp4

# Mix gateways: chat via OpenAI direct, video via TokenRouter (the default)
python $SKILL generate \
  --transcript-file episode.txt \
  --chat-base-url https://api.openai.com/v1 \
  --chat-api-key $OPENAI_KEY \
  --chat-model gpt-4o \
  --out clip.mp4
```

## Flags

| Flag | Default | Description |
|---|---|---|
| `--transcript-file` | _required_ | Path to a `.txt` / `.md` file holding the podcast transcript. |
| `--image` | _(none)_ | Optional public URL of a hero image. Seeds the first beat's `first_frame_image`. |
| `--length` | `15` (or config) | Target video length in seconds. |
| `--num-beats` | `1` (or config) | Dialogue turns. Must be `1` or even ≥ 2; `length / num_beats` must land in `[4, 15]`. |
| `--out` | `out.mp4` | Output path. |
| `--api-key` | _(env / config)_ | Top-level fallback API key any layer inherits. |
| `--base-url` | _(env / config)_ | Top-level fallback gateway URL. |
| `--chat-model` | _(config)_ | Chat model id override. |
| `--chat-base-url` | _(config)_ | Chat gateway URL override. |
| `--chat-api-key` | _(config)_ | Chat key override. |
| `--video-model` | _(config)_ | Video model id override. |
| `--video-base-url` | _(config)_ | Video gateway URL override. |
| `--video-api-key` | _(config)_ | Video key override. |

## Resolution precedence

For every field (`base_url`, `api_key`, model id, length, num_beats):

```
CLI flag  >  env var  >  ~/.config/podcast-to-tiktok/config.json  >  built-in default
```

Per-layer config overrides top-level config, so you can have a top-level TokenRouter key and a separate OpenAI key just for chat.

## Environment variables

| Var | Purpose |
|---|---|
| `OPENAI_API_KEY` | Top-level bearer token; used when a layer has no key of its own. |
| `CHAT_API_KEY` / `VIDEO_API_KEY` | Per-layer keys. |
| `TOKENROUTER_BASE_URL` | Top-level base URL fallback (default `https://api.tokenrouter.com/v1`). |
| `CHAT_BASE_URL` / `VIDEO_BASE_URL` | Per-layer base URLs. |
| `CHAT_MODEL` / `VIDEO_MODEL` | Per-layer model ids. |

## Output

- `out.mp4` at 720×1280, H.264 + AAC, exactly `--length` seconds.
- Each beat is one Seedance clip (native 9:16, native lip-synced dialogue audio). Concatenation is a plain video+audio concat — no letterbox-blur, no audio mux.
- Typical runtime: ~2 min for the default 1-beat run; ~2 min × `num_beats` for multi-beat (sequential chain).

## Pipeline (what the script does)

1. Sends the transcript to the chat model and asks for JSON: two animal characters (each with a `voice_description`), shared art-direction `style`, and `num_beats` beats. Each beat carries its own `video_prompt`.
2. Submits Seedance 2.0 Fast tasks **sequentially**, one per beat. Inline `--ratio 9:16 --duration N --resolution 720p` flags follow the prompt body (Midjourney-style; JSON-body params like `duration` are silently ignored on TokenRouter). The prompt wraps the line in `"quoted speech"` so Seedance can lip-sync the dialogue audio. For multi-beat runs, after each beat the last frame is extracted, uploaded to litterbox.catbox.moe (1h auto-expiry; falls back to catbox.moe if litterbox 4xxs), and used as `first_frame_image` for the next beat — keeps cast consistent across cuts.
3. Concatenates the clips (video + native audio) with ffmpeg into a single 720×1280 MP4.

> **Audio reliability note:** Seedance's native dialogue audio is empirically uneven on this gateway — sometimes a beat comes back with full lip-synced speech, sometimes ambient-only. Retry the run if the first attempt is silent.

## Known limitations

- Native output is 720×1280. If you need 1080p, post-process with an ffmpeg upscale.
- Seedance clip durations are bounded to **4–15 seconds**. The script dies up front if `--length / --num-beats` lands outside that range.
- Voice control is **prompt-driven**, not a fixed list. The chat model invents a `voice_description` per character ("high-pitched, fast, excited, like a kid…") which gets woven into the Seedance prompt. There's no `alloy / nova / onyx` selector anymore.
- `--image` must be publicly fetchable by the gateway; private / signed URLs will fail submission.
- The `/video/generations` path is not part of the OpenAI spec — the chat layer is portable to any OpenAI-compatible gateway, but the video layer needs a gateway that exposes Seedance via that path (TokenRouter does).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `error: ffmpeg not found on PATH` | Install ffmpeg (`brew install ffmpeg` on macOS, `apt install ffmpeg` on Linux). |
| `error: no API key for chat / video` | Run `python skills/podcast-to-tiktok/generate.py setup` to save a key, or pass `--chat-api-key` / `--video-api-key` / `OPENAI_API_KEY`. |
| `error: each beat would be N.Ns; Seedance only supports 4-15s per clip` | `--length / --num-beats` is out of range. Either lower `--num-beats` or raise `--length`. |
| `warning: <host> is not known to serve /video/generations` | The video layer's `base_url` is OpenAI direct or OpenRouter, which don't expose video generation. Point video at TokenRouter. |
| 401 on previously-working keys | If you migrated from Pale Blue Dot (now sunset), regenerate your key on TokenRouter — old keys won't work. |
| `ffmpeg failed` | Check that `ffmpeg` is on `PATH` and supports `libx264` + `aac` (default Homebrew build does). |

## License

MIT — see [LICENSE](LICENSE).
