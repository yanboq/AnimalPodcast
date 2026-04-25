#!/usr/bin/env python3
"""Podcast transcript -> N-beat 9:16 cartoon-animal-dialogue TikTok.

Uses Claude (chat) for dialogue scripting and Seedance 2.0 Fast (video) for
720x1280 9:16 clips with native lip-synced dialogue audio. Each layer goes
through an OpenAI-compatible gateway; chat and video can target different
gateways via per-layer overrides.

Subcommands:
  setup     interactive first-run configuration wizard
  generate  produce a video from a transcript (default if no subcommand)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx


# ---------------------------------------------------------------------------
# Defaults + curated provider/model menus
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://api.tokenrouter.com/v1"
DEFAULT_CHAT_MODEL = "anthropic/claude-opus-4.6"
DEFAULT_VIDEO_MODEL = "dreamina-seedance-2-0-fast-260128"
# Default sized for a punchy TikTok: a single 15s beat at Seedance's max clip
# duration. Multi-beat runs (--num-beats 2/4/...) chain visually via last-frame
# seeding so the cast stays consistent across cuts.
SEEDANCE_MAX_CLIP_SECONDS = 15
SEEDANCE_MIN_CLIP_SECONDS = 4
DEFAULT_NUM_BEATS = 1
DEFAULT_LENGTH = SEEDANCE_MAX_CLIP_SECONDS * DEFAULT_NUM_BEATS  # 15s

CONFIG_PATH = pathlib.Path.home() / ".config" / "podcast-to-tiktok" / "config.json"

# Update when adding providers / when gateways change naming.
PROVIDERS = {
    "tokenrouter": {
        "label": "TokenRouter (default)",
        "base_url": "https://api.tokenrouter.com/v1",
        "chat_models": [
            "anthropic/claude-opus-4.6",
            "anthropic/claude-sonnet-4.6",
            "anthropic/claude-haiku-4.5",
        ],
        "video_models": [
            "dreamina-seedance-2-0-fast-260128",
            "dreamina-seedance-2-0-260128",
        ],
    },
    "openai": {
        "label": "OpenAI direct",
        "base_url": "https://api.openai.com/v1",
        "chat_models": ["gpt-4o", "gpt-4o-mini"],
        "video_models": [],
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "chat_models": [
            "anthropic/claude-opus-4.6",
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-4o",
        ],
        "video_models": [],
    },
}

# Hosts that don't serve a given layer. Used to warn users before network calls.
LAYER_BAD_HOSTS = {
    "video": ("api.openai.com", "openrouter.ai"),
}


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


class VideoTaskFailed(Exception):
    """Raised by poll_video when the gateway reports a terminal failure for a task."""


@dataclass
class Endpoint:
    base_url: str
    api_key: str

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        die(f"config file at {CONFIG_PATH} is not valid JSON: {e}")
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    os.chmod(CONFIG_PATH, 0o600)


def resolve_layer(layer: str, args, env, config) -> tuple[str, str | None, str]:
    """Returns (base_url, api_key, model) for the layer.

    Precedence: CLI flag > env var > per-layer config > top-level config > built-in default.
    """
    layer_cfg = config.get(layer, {}) or {}
    top_base = config.get("base_url") or DEFAULT_BASE_URL
    top_key = config.get("api_key")

    cli_base = getattr(args, f"{layer}_base_url", None) or args.base_url
    cli_key = getattr(args, f"{layer}_api_key", None) or args.api_key
    cli_model = getattr(args, f"{layer}_model", None)

    base_url = (
        cli_base
        or env.get(f"{layer.upper()}_BASE_URL")
        or env.get("TOKENROUTER_BASE_URL")
        or layer_cfg.get("base_url")
        or top_base
    )
    api_key = (
        cli_key
        or env.get(f"{layer.upper()}_API_KEY")
        or env.get("OPENAI_API_KEY")
        or layer_cfg.get("api_key")
        or top_key
    )
    default_model = DEFAULT_CHAT_MODEL if layer == "chat" else DEFAULT_VIDEO_MODEL
    model = (
        cli_model
        or env.get(f"{layer.upper()}_MODEL")
        or layer_cfg.get("model")
        or default_model
    )
    return base_url, api_key, model


# ---------------------------------------------------------------------------
# Dialogue scripting (Claude via chat completions)
# ---------------------------------------------------------------------------

def dialogue_prompt(transcript: str, num_beats: int, slot_seconds: float) -> str:
    # Natural conversational pace ~ 2.6 words/sec; aim ~92% of the slot so lines
    # don't run long but also don't leave awkward silence at the end of each beat.
    target_words = max(10, int(slot_seconds * 2.6 * 0.92))
    takeaways = max(1, num_beats // 2)
    if num_beats == 1:
        intro = (
            "Pick the SINGLE most insightful, specific, surprising takeaway from the transcript "
            "— a concrete number, named tactic, counterintuitive claim, or money figure — and have "
            "ONE cartoon animal deliver it as a punchy hot take to a second cartoon animal who is "
            "in frame but does not speak. Don't summarize the whole episode."
        )
    else:
        intro = (
            f"Find the {takeaways} most insightful, specific, surprising takeaways — concrete numbers, "
            f"named tactics, counterintuitive claims, money figures — and turn them into a {num_beats}-turn "
            f"dialogue between two cartoon animals. Don't summarize the whole episode."
        )
    return f"""You turn podcast transcripts into short TikTok videos where two cartoon animals
discuss the podcast's most valuable ideas.

{intro}

Return strict JSON:
{{
  "character_a": {{
    "animal": "<single animal>",
    "voice_description": "<vivid description of how this character SOUNDS — pitch, pace, energy, accent, e.g. 'high-pitched, fast, excited, like a kid who just figured out a magic trick'>",
    "look": "<short visual description, e.g. 'scruffy raccoon with round glasses and a tiny notebook'>"
  }},
  "character_b": {{
    "animal": "<different animal>",
    "voice_description": "<distinctly different sound, e.g. 'deep, slow, gravelly, calm — like a tired bartender'>",
    "look": "..."
  }},
  "style": "Shared art-direction phrase every beat inherits, e.g. 'Studio Ghibli cartoon, warm painterly lighting, soft palette, vertical 9:16 framing'.",
  "beats": [
    {{
      "speaker": "a" or "b",
      "line": "~{target_words} words; must cite a SPECIFIC fact/number/tactic from the transcript",
      "video_prompt": "One rich cartoon scene that VISUALLY DEPICTS the topic of this line. Include concrete props, location, what each animal is doing. Invent visual metaphors. COMPOSITION: both animals close together, centered horizontally in the middle third, framed chest-up; avoid wide shots, split staging, or characters near the edges."
    }},
    ... exactly {num_beats} beats, alternating a, b, a, b, ...
  ]
}}

Hard rules:
- Beats MUST alternate speakers starting with a.
- Every line cites something concrete (dollar figure, person's name, tactic, product, specific step). NO generic reactions.
- Each line is ~{target_words} words to fill ~{slot_seconds:.1f}s of speech.
- Each video_prompt depicts the specific topic of ITS line.
- Reuse the two animals + the `style` for continuity, but CHANGE background/props/staging every beat.
- voice_description must clearly differentiate the two characters.
- NO on-screen text, captions, logos, or watermarks inside any video_prompt.

Transcript:
<<<
{transcript}
>>>
"""


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
    return {}


def pick_dialogue(client: httpx.Client, ep: Endpoint, model: str,
                  transcript: str, num_beats: int, slot_seconds: float) -> dict:
    resp = client.post(
        f"{ep.base_url}/chat/completions",
        headers=ep.headers,
        json={
            "model": model,
            "messages": [{"role": "user", "content": dialogue_prompt(transcript, num_beats, slot_seconds)}],
            "response_format": {"type": "json_object"},
        },
        timeout=180,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    data = _extract_json(content)

    beats = data.get("beats") or []
    if len(beats) != num_beats:
        die(f"model returned {len(beats)} beats, expected {num_beats}")
    for i, b in enumerate(beats):
        b["speaker"] = "a" if i % 2 == 0 else "b"
    return data


def build_beat_prompt(d: dict, beat_idx: int) -> str:
    beat = d["beats"][beat_idx]
    a = d["character_a"]
    b = d["character_b"]
    speaking = a if beat["speaker"] == "a" else b
    style = d.get("style") or "Studio Ghibli cartoon, warm painterly lighting, vertical 9:16 framing"

    cast = (
        f"Both characters are always in the shot together: "
        f"a cartoon {a['animal']} ({a.get('look', '')}) "
        f"and a cartoon {b['animal']} ({b.get('look', '')})."
    )
    voice = speaking.get("voice_description") or "natural cartoon voice"
    speaker_animal = speaking["animal"]
    line = beat["line"].replace('"', '\\"')

    return (
        f"{beat['video_prompt']} {cast} "
        f"The {speaker_animal} ({voice}) says: \"{line}\". "
        f"Lip-synced dialogue audio. "
        f"Art direction: {style}. "
        f"No on-screen text, no captions, no logos, no watermarks."
    )


# ---------------------------------------------------------------------------
# Video (Seedance via OpenAI-compatible /video/generations)
# ---------------------------------------------------------------------------

def submit_video(client: httpx.Client, ep: Endpoint, model: str, prompt: str,
                 first_frame_image: str | None, duration: int) -> str:
    # Seedance on TokenRouter takes generation params as Midjourney-style
    # `--flag value` suffix tokens; JSON body fields like `duration` / `aspect_ratio`
    # are silently ignored, and prefixing the flags blanks the prompt's content
    # adherence (model renders unrelated drama footage). Flags must come AFTER.
    full_prompt = f"{prompt} --ratio 9:16 --duration {duration} --resolution 720p"
    payload: dict = {"model": model, "prompt": full_prompt}
    if first_frame_image:
        payload["first_frame_image"] = first_frame_image
    resp = client.post(f"{ep.base_url}/video/generations",
                       headers=ep.headers, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    task_id = data.get("task_id") or data.get("id")
    if not task_id:
        die(f"video submit: no task_id in {data}")
    return task_id


def poll_video(client: httpx.Client, ep: Endpoint, task_id: str, label: str = "") -> str:
    deadline = time.time() + 900
    last_progress = None
    while time.time() < deadline:
        resp = client.get(f"{ep.base_url}/video/generations/{task_id}",
                          headers=ep.headers, timeout=60)
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
        if status in ("FAIL", "FAILED", "FAILURE", "ERROR"):
            inner = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
            err = inner.get("error") or data.get("fail_reason") or data
            raise VideoTaskFailed(str(err))
        time.sleep(10)
    die(f"video task timed out ({label})")
    return ""


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


def extract_last_frame(mp4_path: pathlib.Path, png_path: pathlib.Path) -> None:
    # -sseof seeks relative to EOF; -0.2s lands inside the last visible frame.
    run_ffmpeg([
        "ffmpeg", "-y",
        "-sseof", "-0.2",
        "-i", str(mp4_path),
        "-frames:v", "1",
        "-q:v", "2",
        str(png_path),
    ])


def upload_temp_image(client: httpx.Client, png_path: pathlib.Path) -> str:
    # Hosts the PNG so the gateway can fetch it as a first_frame_image seed.
    # Tries litterbox.catbox.moe first (1h auto-expiry, ideal); falls back to
    # catbox.moe (persistent — files stay until catbox prunes them) if
    # litterbox returns 4xx. Shells out to curl with --http1.1 because both
    # httpx and curl HTTP/2 hit intermittent 412s when reusing connections in
    # long-running sessions. Uploaded content is AI-generated cartoon frames.
    _ = client  # signature parity with the rest of the call sites
    if shutil.which("curl") is None:
        die("curl not found on PATH (needed to upload seed frames)")
    endpoints = [
        ("litterbox", "https://litterbox.catbox.moe/resources/internals/api.php", ["-F", "time=1h"]),
        ("catbox",    "https://catbox.moe/user/api.php",                          []),
    ]
    last_err = ""
    for name, url, extra in endpoints:
        result = subprocess.run(
            ["curl", "-sS", "--fail", "--http1.1",
             "-F", "reqtype=fileupload",
             *extra,
             "-F", f"fileToUpload=@{png_path}",
             url],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0 and result.stdout.strip().startswith("http"):
            return result.stdout.strip()
        last_err = f"{name}: {result.stderr.strip() or result.stdout.strip() or f'rc={result.returncode}'}"
        print(f"      {last_err}; trying next host...", file=sys.stderr, flush=True)
    die(f"all seed-image hosts failed. last error: {last_err}")
    return ""


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

def _input(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or (default or "")


def _yes_no(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    val = input(f"{prompt} {suffix} ").strip().lower()
    if not val:
        return default
    return val.startswith("y")


def _menu(prompt: str, options: list[str], default_idx: int = 0,
          allow_custom: bool = True) -> str:
    print(prompt)
    for i, opt in enumerate(options):
        marker = "*" if i == default_idx else " "
        print(f"  {marker} {i+1}. {opt}")
    if allow_custom:
        print(f"    {len(options)+1}. custom...")
    max_n = len(options) + (1 if allow_custom else 0)
    while True:
        raw = input(f"choose [1-{max_n}, default {default_idx+1}]: ").strip()
        if not raw:
            return options[default_idx]
        try:
            n = int(raw)
        except ValueError:
            print("  please enter a number")
            continue
        if 1 <= n <= len(options):
            return options[n - 1]
        if allow_custom and n == len(options) + 1:
            custom = input("custom value: ").strip()
            if custom:
                return custom
            print("  empty value, try again")
            continue
        print("  out of range")


def _probe_models(base_url: str, api_key: str) -> list[str] | None:
    try:
        with httpx.Client(headers={"Authorization": f"Bearer {api_key}"}, timeout=10) as c:
            r = c.get(f"{base_url}/models")
            if r.status_code == 200:
                data = r.json()
                models = data.get("data") or []
                return [m.get("id") for m in models if m.get("id")]
    except (httpx.HTTPError, KeyboardInterrupt):
        pass
    return None


def _ask_layer(layer: str, provider_keys: list[str], provider_labels: list[str]) -> dict:
    print(f"\n--- {layer} layer ---")
    label = _menu(f"Provider for {layer}:", provider_labels, default_idx=0,
                  allow_custom=False)
    if label.startswith("custom"):
        base_url = _input("base URL")
        models: list[str] = []
    else:
        pkey = provider_keys[provider_labels.index(label)]
        base_url = PROVIDERS[pkey]["base_url"]
        models = PROVIDERS[pkey][f"{layer}_models"]
        if not models:
            print(f"  warning: this provider doesn't appear to support {layer}.")

    api_key = getpass.getpass(f"API key for {base_url}: ").strip()
    if not api_key:
        die(f"no {layer} API key entered")

    default = (DEFAULT_CHAT_MODEL if layer == "chat" else DEFAULT_VIDEO_MODEL)
    if models:
        model = _menu(f"Pick a {layer} model:", models, default_idx=0)
    else:
        model = _input(f"{layer} model id", default=default)
    return {"base_url": base_url, "api_key": api_key, "model": model}


def setup_wizard() -> None:
    print("=" * 60)
    print("podcast-to-tiktok setup")
    print("=" * 60)
    print("This wizard captures your gateway API key and picks default models.")
    print(f"Config will be saved to: {CONFIG_PATH}")
    print()

    if CONFIG_PATH.exists():
        print(f"Existing config detected at {CONFIG_PATH}.")
        choice = _menu(
            "What would you like to do?",
            ["keep existing (exit)", "edit (use existing as defaults)", "rewrite from scratch"],
            default_idx=1, allow_custom=False,
        )
        if choice.startswith("keep"):
            print("Nothing changed.")
            return

    cfg: dict = {}
    same_provider = _yes_no("Use one provider for everything?", default=True)

    provider_keys = list(PROVIDERS.keys())
    provider_labels = [PROVIDERS[k]["label"] for k in provider_keys] + ["custom (enter base URL)"]

    if same_provider:
        provider_label = _menu("Pick a provider:", provider_labels, default_idx=0,
                               allow_custom=False)
        if provider_label.startswith("custom"):
            base_url = _input("base URL", default=DEFAULT_BASE_URL)
            chat_models: list[str] = []
            video_models: list[str] = []
        else:
            pkey = provider_keys[provider_labels.index(provider_label)]
            base_url = PROVIDERS[pkey]["base_url"]
            chat_models = PROVIDERS[pkey]["chat_models"]
            video_models = PROVIDERS[pkey]["video_models"]

        api_key = getpass.getpass(f"API key for {base_url}: ").strip()
        if not api_key:
            die("no API key entered")
        if not api_key.startswith("sk-"):
            print("  warning: key doesn't start with 'sk-'. Continuing anyway.")

        if _yes_no("Probe the gateway to confirm the key works?", default=True):
            probed = _probe_models(base_url, api_key)
            if probed is None:
                print("  could not reach gateway or list models — continuing anyway.")
            else:
                print(f"  ok, gateway reports {len(probed)} models")

        cfg["base_url"] = base_url
        cfg["api_key"] = api_key

        if chat_models:
            cfg["chat"] = {"model": _menu("Pick a chat model:", chat_models, default_idx=0)}
        else:
            cfg["chat"] = {"model": _input("chat model id", default=DEFAULT_CHAT_MODEL)}

        if not video_models:
            print("  warning: this provider's curated list has no video models.")
            cfg["video"] = {"model": _input("video model id", default=DEFAULT_VIDEO_MODEL)}
        else:
            cfg["video"] = {"model": _menu("Pick a video model:", video_models, default_idx=0)}

    else:
        cfg["chat"] = _ask_layer("chat", provider_keys, provider_labels)
        cfg["video"] = _ask_layer("video", provider_keys, provider_labels)

    print()
    while True:
        try:
            length = int(_input("default video length (seconds)", default=str(DEFAULT_LENGTH)))
            num_beats = int(_input("default number of beats (1 or any even number)", default=str(DEFAULT_NUM_BEATS)))
            if num_beats < 1 or (num_beats > 1 and num_beats % 2 != 0):
                print("  num_beats must be 1 or an even number >= 2")
                continue
            slot = length / num_beats
            if slot < SEEDANCE_MIN_CLIP_SECONDS or slot > SEEDANCE_MAX_CLIP_SECONDS:
                print(f"  each beat would be {slot:.1f}s; Seedance only supports {SEEDANCE_MIN_CLIP_SECONDS}-{SEEDANCE_MAX_CLIP_SECONDS}s. Adjust length or num_beats.")
                continue
            break
        except ValueError:
            print("  please enter integers")
    cfg["defaults"] = {"length": length, "num_beats": num_beats}

    if shutil.which("ffmpeg") is None:
        print()
        print("warning: 'ffmpeg' not found on PATH.")
        print("  macOS: brew install ffmpeg")
        print("  linux: apt install ffmpeg")
        print("  Generation will fail until ffmpeg is installed.")

    save_config(cfg)
    print()
    print(f"saved {CONFIG_PATH} (mode 0600)")
    print()
    print("try a test run:")
    print(f"  python {sys.argv[0]} generate --transcript-file <transcript.txt> --out out.mp4")


# ---------------------------------------------------------------------------
# Generate command
# ---------------------------------------------------------------------------

def cmd_generate(args: argparse.Namespace) -> None:
    if shutil.which("ffmpeg") is None:
        die("ffmpeg not found on PATH. Install with 'brew install ffmpeg' (macOS) or 'apt install ffmpeg' (Linux).")

    config = load_config()
    env = os.environ

    # Apply config defaults to length/num_beats only when the user didn't pass a flag.
    defaults = config.get("defaults", {})
    if args.length is None:
        args.length = defaults.get("length", DEFAULT_LENGTH)
    if args.num_beats is None:
        args.num_beats = defaults.get("num_beats", DEFAULT_NUM_BEATS)

    chat_base, chat_key, chat_model = resolve_layer("chat", args, env, config)
    video_base, video_key, video_model = resolve_layer("video", args, env, config)

    if not chat_key:
        die(f"no API key for chat. Run 'python {sys.argv[0]} setup' or pass --chat-api-key / --api-key / OPENAI_API_KEY.")
    if not video_key:
        die(f"no API key for video. Run 'python {sys.argv[0]} setup' or pass --video-api-key / --api-key / OPENAI_API_KEY.")

    for bad in LAYER_BAD_HOSTS.get("video", ()):
        if bad in video_base:
            print(f"warning: {video_base} is not known to serve /video/generations — generation will likely fail.", file=sys.stderr)

    num_beats = args.num_beats
    if num_beats < 1 or (num_beats > 1 and num_beats % 2 != 0):
        die(f"--num-beats must be 1 or an even number >= 2 (got {num_beats})")
    audio_slot = args.length / num_beats
    if audio_slot < SEEDANCE_MIN_CLIP_SECONDS or audio_slot > SEEDANCE_MAX_CLIP_SECONDS:
        die(f"each beat would be {audio_slot:.1f}s; Seedance only supports {SEEDANCE_MIN_CLIP_SECONDS}-{SEEDANCE_MAX_CLIP_SECONDS}s per clip. Adjust --length or --num-beats.")
    clip_seconds = int(round(audio_slot))

    transcript_path = pathlib.Path(args.transcript_file)
    if not transcript_path.exists():
        die(f"transcript file not found: {args.transcript_file}")
    try:
        transcript = transcript_path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as e:
        die(f"transcript file is not valid UTF-8: {e}")
    if not transcript:
        die(f"transcript file is empty: {args.transcript_file}")

    chat_ep = Endpoint(chat_base, chat_key)
    video_ep = Endpoint(video_base, video_key)

    print(f"[1/3] asking {chat_model} for {num_beats}-beat dialogue...", flush=True)
    with httpx.Client() as client:
        d = pick_dialogue(client, chat_ep, chat_model, transcript, num_beats, audio_slot)
        print(f"      cast: {d['character_a']['animal']} vs {d['character_b']['animal']}")
        print(f"      style: {d.get('style', '(default)')}")
        for i, beat in enumerate(d["beats"]):
            print(f"      beat {i+1} [{beat['speaker']}]: {beat['line']}")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            beat_mp4s = [tmp / f"beat_{i}.mp4" for i in range(num_beats)]

            # Beats render sequentially; each beat's last frame seeds the next
            # beat's first_frame_image so the cast/scene stay consistent across cuts.
            print(f"[2/3] generating {num_beats} {video_model} clip(s) (chained)...", flush=True)
            seed_url: str | None = args.image
            max_attempts = 3
            for i in range(num_beats):
                prompt = build_beat_prompt(d, i)
                last_err: Exception | None = None
                for attempt in range(1, max_attempts + 1):
                    print(f"      beat {i+1}: submitting (attempt {attempt}/{max_attempts})...", flush=True)
                    tid = submit_video(client, video_ep, video_model, prompt, seed_url, clip_seconds)
                    print(f"      beat {i+1}: task={tid}", flush=True)
                    try:
                        result_url = poll_video(client, video_ep, tid, f"beat {i+1}")
                        break
                    except VideoTaskFailed as e:
                        last_err = e
                        print(f"      beat {i+1}: task failed ({e}); retrying...", flush=True)
                else:
                    die(f"beat {i+1} failed {max_attempts} times: {last_err}. Often a transient moderation false-positive — rerun, or rephrase the line.")
                download(client, result_url, beat_mp4s[i])
                if i < num_beats - 1:
                    frame_png = tmp / f"beat_{i}_last.png"
                    extract_last_frame(beat_mp4s[i], frame_png)
                    seed_url = upload_temp_image(client, frame_png)
                    print(f"      beat {i+1}: last frame -> seed for beat {i+2}", flush=True)

            print(f"[3/3] concatenating {num_beats} clip(s) (video + native audio)...", flush=True)
            ff = ["ffmpeg", "-y"]
            for mp4 in beat_mp4s:
                ff += ["-i", str(mp4)]
            inputs_chain = "".join(f"[{i}:v][{i}:a]" for i in range(num_beats))
            ff += [
                "-filter_complex", f"{inputs_chain}concat=n={num_beats}:v=1:a=1[v][a]",
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-c:a", "aac",
                "-t", str(args.length),
                args.out,
            ]
            run_ffmpeg(ff)

    print(f"done: {args.out}")


# ---------------------------------------------------------------------------
# Argparse + entrypoint
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="generate.py")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("setup", help="interactive first-run configuration wizard")

    g = sub.add_parser("generate", help="produce a video from a transcript")
    g.add_argument("--transcript-file", required=True)
    g.add_argument("--image", default=None,
                   help="Optional public URL of a hero image (seed for first beat's first frame)")
    g.add_argument("--out", default="out.mp4")
    g.add_argument("--length", type=int, default=None,
                   help=f"Target video length in seconds (default {DEFAULT_LENGTH} or config)")
    g.add_argument("--num-beats", type=int, default=None,
                   help=f"Dialogue turns (default {DEFAULT_NUM_BEATS} or config; 1 or any even number)")
    g.add_argument("--api-key", default=None, help="Top-level API key fallback")
    g.add_argument("--base-url", default=None, help="Top-level base URL fallback")
    g.add_argument("--chat-model", default=None)
    g.add_argument("--chat-base-url", default=None)
    g.add_argument("--chat-api-key", default=None)
    g.add_argument("--video-model", default=None)
    g.add_argument("--video-base-url", default=None)
    g.add_argument("--video-api-key", default=None)

    return p


def main() -> None:
    # Backwards-compat shim: bare `python generate.py --transcript-file ...`
    # routes to `generate` so existing scripts keep working.
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["generate", *argv]

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "setup":
        setup_wizard()
        return
    if args.command == "generate":
        cmd_generate(args)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
