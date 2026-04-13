#!/usr/bin/env python3
"""Podcast transcript -> N-beat 9:16 cartoon-animal-dialogue TikTok.

All model traffic goes through an OpenAI-compatible gateway (palebluedot)
with a single bearer token in OPENAI_API_KEY.

Endpoints used on the gateway:
  POST /chat/completions                       -> dialogue JSON + streaming audio (PCM16)
  POST /video/generations                      -> submit Hailuo 2.3 task
  GET  /video/generations/{task_id}            -> poll; response has data.result_url on SUCCESS
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE_URL = os.environ.get("PALEBLUEDOT_BASE_URL", "https://open.palebluedot.ai/v1")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "anthropic/claude-opus-4.6")
TTS_MODEL = os.environ.get("TTS_MODEL", "openai/gpt-audio")
VIDEO_MODEL = os.environ.get("VIDEO_MODEL", "MiniMax-Hailuo-2.3")

# gpt-audio streaming returns mono 16-bit little-endian PCM at 24 kHz.
PCM_SAMPLE_RATE = 24000

VOICES = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]


def dialogue_prompt(transcript: str, num_beats: int, slot_seconds: float) -> str:
    # Natural conversational pace ~ 2.6 words/sec. Hit ~90% of the slot so lines
    # don't run long but also don't leave awkward silence at the end of each beat.
    target_words = max(10, int(slot_seconds * 2.6 * 0.92))
    return f"""You turn podcast transcripts into short TikTok videos where two cartoon animals
discuss the podcast's most valuable ideas.

Your job is NOT to summarize the whole episode. Your job is to find the {num_beats // 2}
most insightful, specific, surprising takeaways — concrete numbers, named tactics,
counterintuitive claims, or money figures that a viewer will actually remember —
and turn them into a {num_beats}-turn dialogue between two cartoon animals.

Return strict JSON:
{{
  "character_a": {{"animal": "<single animal>", "voice": "<one of: alloy, echo, fable, onyx, nova, shimmer>", "look": "short visual description, e.g. 'scruffy raccoon with round glasses and a tiny notebook'"}},
  "character_b": {{"animal": "<different animal>", "voice": "<different voice>", "look": "short visual description"}},
  "style": "Shared art-direction phrase that every beat inherits, e.g. 'Studio Ghibli cartoon, warm painterly lighting, soft palette, vertical 9:16 framing'.",
  "beats": [
    {{
      "speaker": "a" or "b",
      "line": "~{target_words} words; must cite a SPECIFIC fact/number/tactic from the transcript",
      "video_prompt": "One rich cartoon scene that VISUALLY DEPICTS the topic of this line. Include concrete props, location, and what each animal is doing. Invent visual metaphors (stacks of dollar bills, a glowing phone, a 'NOW' wristwatch). Do NOT repeat the same scene across beats. COMPOSITION: both animals must be close together, centered horizontally in the middle third of the frame, framed from the chest or waist up; avoid wide shots, split staging, or characters near the left/right edges."
    }},
    ... exactly {num_beats} beats, strictly alternating a, b, a, b, ...
  ]
}}

Hard rules:
- Beats MUST alternate speakers starting with a.
- Every line cites something concrete from the transcript: a dollar figure, a person's name, a tactic, a product, a specific step. NO generic reactions ("that's wild", "love it").
- Each line is ~{target_words} words so it fills ~{slot_seconds:.1f}s of speech (don't undershoot — we want the audio to fill the beat, not leave silence).
- Each video_prompt MUST depict the specific topic of ITS line — if the line mentions a $29 watch, show a watch; if it mentions Reddit, show a phone with a subreddit-looking screen. Reuse the two animals and the `style` for continuity, but CHANGE the background, props, and staging every beat.
- voice_a != voice_b. Both in: alloy, echo, fable, onyx, nova, shimmer.
- NO on-screen text, captions, logos, or watermarks inside any video_prompt.

Transcript:
<<<
{transcript}
>>>
"""


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _extract_json(text: str) -> dict:
    """Tolerate markdown fences and trailing prose around a JSON object."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0].strip()
        if s.startswith("json\n"):
            s = s[5:]
    start = s.find("{")
    if start < 0:
        die(f"no JSON object found in model response:\n{text[:500]}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start : i + 1])
                    except json.JSONDecodeError as e:
                        die(f"JSON parse error: {e}\n{s[start:i+1][:800]}")
    die(f"unbalanced JSON in model response:\n{text[:500]}")


def pick_dialogue(client: httpx.Client, transcript: str, num_beats: int, slot_seconds: float) -> dict:
    resp = client.post(
        f"{BASE_URL}/chat/completions",
        json={
            "model": CHAT_MODEL,
            "messages": [{"role": "user", "content": dialogue_prompt(transcript, num_beats, slot_seconds)}],
            "response_format": {"type": "json_object"},
        },
        timeout=180,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    data = _extract_json(content)

    va = data["character_a"]["voice"]
    vb = data["character_b"]["voice"]
    if va not in VOICES or vb not in VOICES or va == vb:
        data["character_a"]["voice"] = "onyx"
        data["character_b"]["voice"] = "nova"

    beats = data.get("beats") or []
    if len(beats) != num_beats:
        die(f"model returned {len(beats)} beats, expected {num_beats}")
    # enforce alternation
    for i, b in enumerate(beats):
        expected = "a" if i % 2 == 0 else "b"
        b["speaker"] = expected
    return data


def tts_line(client: httpx.Client, text: str, voice: str, out_pcm: pathlib.Path) -> None:
    """Stream gpt-audio via chat-completions. Writes raw PCM16 mono 24kHz bytes."""
    payload = {
        "model": TTS_MODEL,
        "modalities": ["text", "audio"],
        "audio": {"voice": voice, "format": "pcm16"},
        "stream": True,
        "messages": [
            {"role": "user", "content": f"Read this aloud, naturally, as a cartoon character:\n\n{text}"},
        ],
    }
    buf = bytearray()
    with client.stream("POST", f"{BASE_URL}/chat/completions", json=payload, timeout=120) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            audio = delta.get("audio") or {}
            b64 = audio.get("data")
            if b64:
                buf.extend(base64.b64decode(b64))
    if not buf:
        die(f"tts: got no audio for voice={voice} text={text!r}")
    out_pcm.write_bytes(bytes(buf))


def build_beat_prompt(d: dict, beat_idx: int) -> str:
    beat = d["beats"][beat_idx]
    a = d["character_a"]
    b = d["character_b"]
    style = d.get("style") or "Studio Ghibli cartoon, warm painterly lighting, vertical 9:16 framing"
    cast = (
        f"Both characters are always in the shot together: "
        f"a cartoon {a['animal']} ({a.get('look', '')}) "
        f"and a cartoon {b['animal']} ({b.get('look', '')})."
    )
    return (
        f"{beat['video_prompt']} "
        f"{cast} "
        f"Art direction: {style}. "
        f"No on-screen text, no captions, no logos, no watermarks."
    )


def submit_video(client: httpx.Client, prompt: str, first_frame_image: str | None, duration: int) -> str:
    payload: dict = {
        "model": VIDEO_MODEL,
        "prompt": prompt,
        "duration": duration,
        "resolution": "1080P",
    }
    if first_frame_image:
        payload["first_frame_image"] = first_frame_image
    resp = client.post(f"{BASE_URL}/video/generations", json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    task_id = data.get("task_id") or data.get("id")
    if not task_id:
        die(f"video submit: no task_id in {data}")
    return task_id


def poll_video(client: httpx.Client, task_id: str, label: str = "") -> str:
    deadline = time.time() + 900
    last_progress = None
    while time.time() < deadline:
        resp = client.get(f"{BASE_URL}/video/generations/{task_id}", timeout=60)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", body)
        status = (data.get("status") or "").upper()
        progress = data.get("progress")
        if progress != last_progress:
            print(f"      [{label}] {status} {progress}", flush=True)
            last_progress = progress
        if status == "SUCCESS":
            url = data.get("result_url") or data.get("video_url") or data.get("download_url")
            if not url:
                die(f"video success but no result_url: {data}")
            return url
        if status in ("FAIL", "FAILED", "ERROR"):
            die(f"video task failed ({label}): {data.get('fail_reason') or data}")
        time.sleep(10)
    die(f"video task timed out ({label})")


def download(client: httpx.Client, url: str, out_path: pathlib.Path) -> None:
    with client.stream("GET", url, timeout=300) as r:
        r.raise_for_status()
        with out_path.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)


def run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        die(f"ffmpeg failed:\n{result.stderr}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript-file", required=True)
    ap.add_argument("--image", default=None,
                    help="Optional public URL of a hero image (seed for first beat's first frame)")
    ap.add_argument("--out", default="out.mp4")
    ap.add_argument("--length", type=int, default=20, help="Target video length in seconds (default 20)")
    ap.add_argument("--num-beats", type=int, default=4, help="Dialogue turns (default 4, must be even)")
    args = ap.parse_args()

    num_beats = args.num_beats
    if num_beats < 2 or num_beats % 2 != 0:
        die(f"--num-beats must be an even number >= 2 (got {num_beats})")
    # Each audio slot gets an equal share of total length.
    audio_slot = args.length / num_beats
    # Hailuo 2.3 only supports 6s or 10s clips.
    clip_seconds_video = 10 if audio_slot > 6 else 6

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        die("OPENAI_API_KEY not set (should be a palebluedot gateway key)")

    transcript = pathlib.Path(args.transcript_file).read_text().strip()
    if not transcript:
        die("transcript file is empty")

    client = httpx.Client(headers={"Authorization": f"Bearer {api_key}"})

    print(f"[1/5] asking claude for {num_beats}-beat dialogue with concrete takeaways...", flush=True)
    d = pick_dialogue(client, transcript, num_beats, audio_slot)
    print(
        f"      cast: {d['character_a']['animal']} ({d['character_a']['voice']}) "
        f"vs {d['character_b']['animal']} ({d['character_b']['voice']})"
    )
    print(f"      style: {d.get('style', '(default)')}")
    for i, beat in enumerate(d["beats"]):
        print(f"      beat {i+1} [{beat['speaker']}]: {beat['line']}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = pathlib.Path(tmpdir)
        beat_pcms = [tmp / f"beat_{i}.pcm" for i in range(num_beats)]
        beat_mp4s = [tmp / f"beat_{i}.mp4" for i in range(num_beats)]

        print(f"[2/5] firing {num_beats} video + {num_beats} tts tasks in parallel...", flush=True)
        with ThreadPoolExecutor(max_workers=max(4, num_beats * 2)) as ex:
            video_futures = []
            for i in range(num_beats):
                prompt = build_beat_prompt(d, i)
                # seed only the first beat from the hero image; later beats render fresh.
                seed = args.image if i == 0 else None
                video_futures.append(ex.submit(submit_video, client, prompt, seed, clip_seconds_video))

            tts_futures = []
            for i, beat in enumerate(d["beats"]):
                voice = d["character_a"]["voice"] if beat["speaker"] == "a" else d["character_b"]["voice"]
                tts_futures.append(ex.submit(tts_line, client, beat["line"], voice, beat_pcms[i]))

            task_ids = [f.result() for f in video_futures]
            for f in tts_futures:
                f.result()
        for i, tid in enumerate(task_ids):
            print(f"      beat {i+1}: video={tid}  audio={beat_pcms[i].stat().st_size}B")

        print(f"[3/5] polling {num_beats} hailuo tasks (parallel)...", flush=True)
        with ThreadPoolExecutor(max_workers=num_beats) as ex:
            urls = list(ex.map(lambda pair: poll_video(client, pair[1], f"beat {pair[0]+1}"),
                               list(enumerate(task_ids))))
        print(f"[4/5] downloading {num_beats} clips...", flush=True)
        with ThreadPoolExecutor(max_workers=num_beats) as ex:
            list(ex.map(lambda p: download(client, p[0], p[1]), list(zip(urls, beat_mp4s))))

        print("[5/5] concat videos + audio + mux...", flush=True)
        # Build a single ffmpeg command: N video inputs, N pcm inputs, concat each,
        # pad each audio to clip_seconds, final mux.
        ff = ["ffmpeg", "-y"]
        for mp4 in beat_mp4s:
            ff += ["-i", str(mp4)]
        for pcm in beat_pcms:
            ff += ["-f", "s16le", "-ar", str(PCM_SAMPLE_RATE), "-ac", "1", "-i", str(pcm)]

        filter_parts = []
        # Trim each Hailuo clip to exactly audio_slot seconds so beats align and
        # the final frame of each beat is inside the natural action window.
        for i in range(num_beats):
            filter_parts.append(
                f"[{i}:v]trim=duration={audio_slot},setpts=PTS-STARTPTS[v{i}]"
            )
        vcat_in = "".join(f"[v{i}]" for i in range(num_beats))
        filter_parts.append(f"{vcat_in}concat=n={num_beats}:v=1:a=0[vcat]")
        # Hailuo only emits 16:9 landscape. Convert to 9:16 TikTok by fitting the
        # full landscape frame to width and filling the vertical padding with a
        # blurred upscale of the same footage. No content is cropped out.
        filter_parts.append("[vcat]split=2[vmain][vbg]")
        filter_parts.append(
            "[vbg]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,gblur=sigma=30[vbgb]"
        )
        filter_parts.append("[vmain]scale=1080:-2[vmains]")
        filter_parts.append("[vbgb][vmains]overlay=(W-w)/2:(H-h)/2[v]")
        acat_in = ""
        for i in range(num_beats):
            audio_idx = num_beats + i
            filter_parts.append(f"[{audio_idx}:a]apad=whole_dur={audio_slot}[a{i}]")
            acat_in += f"[a{i}]"
        filter_parts.append(f"{acat_in}concat=n={num_beats}:v=0:a=1[a]")

        ff += [
            "-filter_complex", ";".join(filter_parts),
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-c:a", "aac",
            "-t", str(args.length),
            args.out,
        ]
        run_ffmpeg(ff)

    print(f"done: {args.out}")


if __name__ == "__main__":
    main()
