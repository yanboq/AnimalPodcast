# podcast-to-tiktok

Turns a podcast transcript into a vertical 9:16 TikTok video where two cartoon animals have a quick back-and-forth about the episode's most valuable takeaways.

- **Visuals:** MiniMax Hailuo 2.3 (per-beat cartoon scene depicting that line's topic)
- **Voices:** OpenAI `gpt-audio` (one voice per character, streamed as PCM16)
- **Dialogue:** Claude picks animals + writes lines pulled from concrete facts in the transcript
- **All API traffic** goes through a single OpenAI-compatible gateway (default: `https://open.palebluedot.ai/v1`)

## Install

### In OpenClaw

```bash
openclaw install https://github.com/yanboq/AnimalPodcast
```

### Manually

```bash
git clone https://github.com/yanboq/AnimalPodcast
cd podcast-to-tiktok
pip install -r requirements.txt
```

## Prerequisites

- Python 3.9+
- `ffmpeg` on `PATH` (macOS: `brew install ffmpeg`)
- A **palebluedot.ai API token** — see below

### Getting a palebluedot.ai token

This skill does **not** use a direct OpenAI / Anthropic / MiniMax key. All three model calls (dialogue, voice, video) route through a single OpenAI-compatible gateway from Pale Blue Dot, which exposes `anthropic/claude-opus-4.6`, `openai/gpt-audio`, and `MiniMax-Hailuo-2.3` behind one endpoint with one token.

1. Go to **[https://www.palebluedot.ai](https://www.palebluedot.ai)** and sign up / sign in.
2. Create an API token in the dashboard (format: `sk-...`).
3. Export it as `OPENAI_API_KEY` before running the skill:

   ```bash
   export OPENAI_API_KEY=sk-...your-palebluedot-token...
   ```

- Website: [https://www.palebluedot.ai](https://www.palebluedot.ai)
- API base URL: `https://open.palebluedot.ai/v1` (already the skill's default)

> The env var is named `OPENAI_API_KEY` because the gateway speaks the OpenAI wire format — but the value **must be a palebluedot.ai token**, not an `openai.com` key. An `openai.com` key will be rejected by the gateway.

To point at a different OpenAI-compatible gateway, override `PALEBLUEDOT_BASE_URL` plus the three model-id env vars documented in [Environment variables](#environment-variables).

## Usage

```bash
export OPENAI_API_KEY=sk-...
python generate.py \
  --transcript-file episode.txt \
  --image https://example.com/hero.jpg \
  --length 30 \
  --num-beats 6 \
  --out out.mp4
```

### Examples

```bash
# Quick 20-second clip, 4 dialogue turns
python generate.py --transcript-file episode.txt --length 20 --num-beats 4 --out clip.mp4

# 30-second clip with a hero image seeding the first beat
python generate.py \
  --transcript-file episode.txt \
  --image https://cdn.example.com/ep733.png \
  --length 30 --num-beats 6 \
  --out clip.mp4
```

## Flags

| Flag | Default | Description |
|---|---|---|
| `--transcript-file` | _required_ | Path to a `.txt` / `.md` file holding the podcast transcript. |
| `--image` | _(none)_ | Optional public URL of a hero image. Seeds the first Hailuo beat's `first_frame_image`. |
| `--length` | `20` | Target video length in seconds. |
| `--num-beats` | `4` | Dialogue turns (must be even; 2, 4, 6, 8, …). `--length / --num-beats` is each beat's audio slot. |
| `--out` | `out.mp4` | Output path. |

## Environment variables

| Var | Purpose |
|---|---|
| `OPENAI_API_KEY` | Bearer token for the gateway. **Required.** |
| `PALEBLUEDOT_BASE_URL` | Override gateway base URL (default `https://open.palebluedot.ai/v1`). |
| `CHAT_MODEL` | Override dialogue model (default `anthropic/claude-opus-4.6`). |
| `TTS_MODEL` | Override audio model (default `openai/gpt-audio`). |
| `VIDEO_MODEL` | Override video model (default `MiniMax-Hailuo-2.3`). |

## Output

- `out.mp4` at 1080×1920, H.264 + AAC, exactly `--length` seconds.
- Each beat is one Hailuo clip scaled to width with a blurred letterbox filling the vertical padding — nothing is cropped out of frame.
- Typical runtime is 90 s – 3 min, dominated by Hailuo rendering (tasks run in parallel).

## Pipeline (what the script does)

1. Sends the transcript to Claude asking for strict JSON: two animal characters, shared art-direction `style`, and `num_beats` dialogue beats. Each beat carries its own `video_prompt` that must visually depict that line's topic.
2. In parallel: submits one Hailuo 2.3 task per beat and streams one `gpt-audio` PCM16 TTS per line.
3. Polls all Hailuo tasks concurrently, downloads the rendered clips.
4. Trims each clip to the audio slot, concats the beats, applies a blurred-letterbox 9:16 transform, pads each audio line to its slot, concats the dialogue, muxes video + audio, and writes the final mp4.

## Known limitations

- Hailuo 2.3 only renders landscape (1366×768 at `1080P`). The script compensates with a blurred-letterbox layout — two horizontal seams are visible where the sharp band meets the blur.
- Hailuo clip durations are limited to 6 s or 10 s. The script picks 6 s by default; if `--length / --num-beats > 6`, it uses 10 s clips.
- `--image` must be publicly fetchable by Hailuo; private / signed URLs will fail the submission.
- Gateway model ids (`anthropic/claude-opus-4.6`, `openai/gpt-audio`, `MiniMax-Hailuo-2.3`) are palebluedot-specific — override via env if your gateway uses different names.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `error: OPENAI_API_KEY not set` | Export the gateway key before running. |
| `HTTP 400 hailuo api error: invalid params ... duration` | Your `--length / --num-beats` landed on an unsupported clip length. Use multiples where the per-beat slot is ≤ 6 (clip = 6 s) or ≤ 10 (clip = 10 s). |
| `tts: got no audio` | Gateway returned no audio deltas for that line. Rerun — transient upstream error. |
| `ffmpeg failed` | Check that `ffmpeg` is on `PATH` and supports `libx264` + `libmp3lame` (default Homebrew build does). |
