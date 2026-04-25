---
name: podcast-to-tiktok
description: Use when the user asks to turn a podcast transcript (or podcast URL) into a short cartoon TikTok — e.g. "turn this podcast into a cartoon TikTok", "make an animal-dialogue short from this transcript", "generate a cartoon video from this podcast". By default produces a 15-second 720x1280 9:16 MP4 — one cartoon animal delivers the punchiest takeaway as a hot take to a silent second animal in frame. Video and lip-synced dialogue audio are generated together by Seedance 2.0 Fast; Claude scripts the line. Multi-beat dialogues (--num-beats 2/4/...) chain visually via last-frame seeding. All API traffic flows through OpenAI-compatible gateways (default: TokenRouter).
metadata: {"requires":"python3.9+, ffmpeg, tokenrouter-api-key"}
---

# podcast-to-tiktok

Activate when the user asks to:

- "turn this podcast into a cartoon TikTok"
- "make an animal-dialogue short from this transcript"
- "generate a cartoon video from this podcast"

## Onboarding (do this first)

Before invoking the script, check whether `~/.config/podcast-to-tiktok/config.json` exists.

- **If it does not exist**, tell the user: *"I need to run a one-time setup to capture your TokenRouter API key and pick models. Run: `python skills/podcast-to-tiktok/generate.py setup`."* The wizard is interactive (it reads stdin and uses `getpass` for the key), so the user must run it themselves — do not try to drive it from your turn.
- **If it exists**, proceed to generate.

The wizard saves keys + model choices to `~/.config/podcast-to-tiktok/config.json` (mode `0600`).

## Inputs

- **Transcript file** (`.txt`/`.md`) — required. A path the script can read.
- **Hero image URL** — optional. If provided, it seeds the first beat's first frame. Must be publicly fetchable.

If the user gives a podcast URL, first extract the transcript text into a local file (e.g. `transcript.txt`) and optionally find the `og:image` URL, then invoke the script.

## Invocation

```bash
python skills/podcast-to-tiktok/generate.py generate \
  --transcript-file <path> \
  [--image <hero-url>] \
  [--length 15] [--num-beats 1] \
  --out out.mp4
```

The bare form (no `generate` subcommand) still works for backwards compatibility:

```bash
python skills/podcast-to-tiktok/generate.py --transcript-file <path> --out out.mp4
```

## Defaults

- **Length:** 15 seconds (= 1 beat × 15s, Seedance's max clip duration).
- **Beats:** 1 (single hot-take line). Must be 1 or an even number ≥ 2.
- **Per-beat duration constraint:** Seedance only supports 4–15s clips, so `length / num_beats` must land in `[4, 15]`. The script dies up front if it doesn't.

For longer videos: `--length 30 --num-beats 2`, `--length 60 --num-beats 4`, etc. (each 15s/beat). Multi-beat runs render **sequentially** and chain via last-frame seeding for character continuity — wall time scales linearly with `num_beats` (~2 min/beat).

## Per-invocation overrides

Useful flags when the user wants to deviate from the saved config without re-running setup:

| Flag | Purpose |
|---|---|
| `--chat-model` / `--video-model` | Override the model id for that layer. |
| `--chat-base-url` / `--video-base-url` | Point that layer at a different OpenAI-compatible gateway. |
| `--chat-api-key` / `--video-api-key` | Use a different key for that layer. |
| `--api-key` / `--base-url` | Top-level fallback that any layer inherits. |

If the user asks for a faster / cheaper run, pass `--chat-model anthropic/claude-sonnet-4.6` (or `anthropic/claude-haiku-4.5`). The video model dominates runtime, so chat-model swaps mostly affect cost, not wall time.

## Environment

The wizard saves these into `~/.config/podcast-to-tiktok/config.json`. Env vars override the file; CLI flags override env vars.

- `OPENAI_API_KEY` — top-level fallback bearer token (used when a layer doesn't have its own key).
- `CHAT_API_KEY` / `VIDEO_API_KEY` — per-layer keys.
- `TOKENROUTER_BASE_URL` — top-level base URL fallback (default `https://api.tokenrouter.com/v1`).
- `CHAT_BASE_URL` / `VIDEO_BASE_URL` — per-layer base URLs.
- `CHAT_MODEL` / `VIDEO_MODEL` — per-layer model ids.

## Dependencies

- Python 3.9+, `httpx` (`pip install -r skills/podcast-to-tiktok/requirements.txt`)
- `ffmpeg` on `PATH` (used only to concat per-beat clips; audio comes inside each clip).

## What the script does

1. Sends the transcript to the chat model and asks for JSON: two animal characters (each with a free-text `voice_description`), a shared art-direction `style`, and `num_beats` beats. Each beat carries its own `video_prompt`.
2. Submits one Seedance 2.0 Fast task per beat, **sequentially**. Inline `--ratio 9:16 --duration N --resolution 720p` flags go after the prompt body (Midjourney-style; JSON body params like `duration` are silently ignored on TokenRouter). The prompt includes the speaker's `voice_description` and the line as `"quoted speech"` so the model produces lip-synced dialogue inside the clip. For multi-beat runs, after each beat extracts the last frame with ffmpeg, uploads it to litterbox.catbox.moe (1h auto-expiry; falls back to catbox.moe if litterbox 4xxs), and passes the URL as `first_frame_image` for the next beat. Locks character/style continuity across cuts.
3. Concatenates all clips (video + native audio) with ffmpeg into a single 720×1280 MP4.

Typical runtime: ~2 min for the default 1-beat run; ~2 min × `num_beats` for multi-beat (sequential chain).

> **Audio reliability note:** Seedance's native dialogue audio rendering is empirically uneven on this gateway — sometimes a beat comes back with full lip-synced speech, sometimes ambient-only. Retry the run if the first attempt's audio is silent.
