from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
import wave
from pathlib import Path
from typing import Any, Optional

import numpy as np

try:
    import soundfile as sf
except ImportError:
    sf = None  # type: ignore[assignment]

try:
    from kokoro_onnx import Kokoro
except ImportError:
    Kokoro = None  # type: ignore[assignment]

import brand  # noqa: E402

PROJECT_DIR = brand.PROJECT_DIR
WORKSPACE_ROOT = brand.get_runtime_root()
KOKORO_MODELS_DIR = brand.get_kokoro_models_dir()

MODEL_CANDIDATE_URLS = [
    (
        "kokoro-v1.0.int8.onnx",
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    ),
    (
        "kokoro-v1.0.onnx",
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    ),
    (
        "kokoro-v1.0.onnx",
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.onnx",
    ),
]

VOICES_CANDIDATE_URLS = [
    (
        "voices-v1.0.bin",
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
    ),
    (
        "voices-v1.0.bin",
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin",
    ),
]

VOICE_CATALOG = [
    {"id": "af_heart", "label": "Heart (US Female — Kokoro Neural)", "lang": "en-us"},
    {"id": "af_sarah", "label": "Sarah (US Female — Kokoro Neural)", "lang": "en-us"},
    {"id": "af_bella", "label": "Bella (US Female — Kokoro Neural)", "lang": "en-us"},
    {"id": "am_adam", "label": "Adam (US Male — Kokoro Neural)", "lang": "en-us"},
    {"id": "am_michael", "label": "Michael (US Male — Kokoro Neural)", "lang": "en-us"},
    {"id": "bf_emma", "label": "Emma (UK Female — Kokoro Neural)", "lang": "en-gb"},
    {"id": "bm_george", "label": "George (UK Male — Kokoro Neural)", "lang": "en-gb"},
    {"id": "sapi_zira", "label": "Microsoft Zira Desktop (Windows SAPI Fallback)", "lang": "en-us"},
]

PAUSE_TAG_RE = re.compile(r"\[pause\s+(\d+(?:\.\d+)?)s?\]", re.IGNORECASE)

_KOKORO_LOCK = threading.Lock()
_KOKORO_CACHE: dict[str, Any] = {"key": None, "instance": None}


def find_ffmpeg_executable() -> Path:
    which_ff = shutil.which("ffmpeg")
    if which_ff:
        return Path(which_ff)
    local_app = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    fallback = local_app / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
    if fallback.is_file():
        return fallback
    winget_pkgs = local_app / "Microsoft" / "WinGet" / "Packages"
    if winget_pkgs.is_dir():
        for pkg_dir in winget_pkgs.glob("*FFmpeg*"):
            for cand in pkg_dir.rglob("ffmpeg.exe"):
                if cand.is_file():
                    return cand
    raise FileNotFoundError("FFmpeg executable not found.")


def _download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "LabGuideStudio/3.0"})
    with urllib.request.urlopen(req, timeout=180) as resp, open(tmp_path, "wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 256)
    tmp_path.replace(dest)


def ensure_kokoro_models(
    models_dir: Path = KOKORO_MODELS_DIR,
    allow_download: bool = False,
) -> tuple[Optional[Path], Optional[Path]]:
    """Locate (and optionally download) Kokoro ONNX model and voice pack on CPU."""
    models_dir.mkdir(parents=True, exist_ok=True)
    onnx_candidates = [
        models_dir / "kokoro-v1.0.int8.onnx",
        models_dir / "kokoro-v1.0.onnx",
    ]
    model_path = next((p for p in onnx_candidates if p.is_file() and p.stat().st_size > 1_000_000), None)
    voices_path = models_dir / "voices-v1.0.bin"
    if not (voices_path.is_file() and voices_path.stat().st_size > 100_000):
        voices_path = None

    if model_path is None and allow_download:
        for fname, url in MODEL_CANDIDATE_URLS:
            dest = models_dir / fname
            try:
                _download_file(url, dest)
                if dest.is_file() and dest.stat().st_size > 1_000_000:
                    model_path = dest
                    break
            except Exception:
                continue

    if voices_path is None and allow_download:
        for fname, url in VOICES_CANDIDATE_URLS:
            dest = models_dir / fname
            try:
                _download_file(url, dest)
                if dest.is_file() and dest.stat().st_size > 100_000:
                    voices_path = dest
                    break
            except Exception:
                continue

    return model_path, voices_path


def _get_kokoro_instance(models_dir: Path = KOKORO_MODELS_DIR, allow_download: bool = False) -> Any:
    if Kokoro is None:
        return None
    model_path, voices_path = ensure_kokoro_models(models_dir=models_dir, allow_download=allow_download)
    if model_path is None or voices_path is None:
        return None

    # Force CPU execution provider to preserve >= 1200 MB GPU VRAM headroom
    os.environ.setdefault("ONNX_PROVIDER", "CPUExecutionProvider")
    cache_key = f"{model_path}|{voices_path}"
    with _KOKORO_LOCK:
        if _KOKORO_CACHE["key"] == cache_key and _KOKORO_CACHE["instance"] is not None:
            return _KOKORO_CACHE["instance"]
        inst = Kokoro(str(model_path), str(voices_path))
        _KOKORO_CACHE["key"] = cache_key
        _KOKORO_CACHE["instance"] = inst
        return inst


def list_available_voices(models_dir: Path = KOKORO_MODELS_DIR) -> dict[str, Any]:
    kokoro = None
    try:
        kokoro = _get_kokoro_instance(models_dir=models_dir, allow_download=False)
    except Exception:
        kokoro = None

    available_ids: set[str] = set()
    if kokoro is not None:
        try:
            available_ids = set(kokoro.get_voices())
        except Exception:
            available_ids = set()

    voices: list[dict[str, str]] = []
    for entry in VOICE_CATALOG:
        if entry["id"] == "sapi_zira":
            voices.append(entry)
        elif kokoro is not None and (not available_ids or entry["id"] in available_ids):
            voices.append(entry)

    return {
        "engine": "kokoro" if kokoro is not None else "sapi",
        "kokoro_ready": kokoro is not None,
        "models_dir": str(models_dir),
        "voices": voices,
    }


def parse_pause_segments(text: str) -> list[tuple[str, Any]]:
    """Split narration text into ('text', str) and ('pause', float_seconds) segments."""
    segments: list[tuple[str, Any]] = []
    pos = 0
    for match in PAUSE_TAG_RE.finditer(text or ""):
        chunk = (text[pos : match.start()]).strip()
        if chunk:
            segments.append(("text", chunk))
        try:
            pause_sec = max(0.1, min(10.0, float(match.group(1))))
        except ValueError:
            pause_sec = 1.0
        segments.append(("pause", pause_sec))
        pos = match.end()
    tail = (text[pos:] if text else "").strip()
    if tail:
        segments.append(("text", tail))
    if not segments:
        segments.append(("text", "Proceed to the next step."))
    return segments


def strip_pause_tags(text: str) -> str:
    return re.sub(r"\s+", " ", PAUSE_TAG_RE.sub(" ", text or "")).strip()


def compute_envelope_30fps(samples: np.ndarray, sample_rate: int) -> list[float]:
    """Compute normalized [0..1] RMS energy envelope at 30 frames per second."""
    if samples.size == 0 or sample_rate <= 0:
        return [0.0]
    mono = samples.astype(np.float32)
    if mono.ndim > 1:
        mono = mono.mean(axis=1)
    hop = max(1, int(round(sample_rate / 30.0)))
    total_frames = max(1, int(math.ceil(len(mono) / hop)))
    raw_rms: list[float] = []
    for i in range(total_frames):
        seg = mono[i * hop : (i + 1) * hop]
        if seg.size == 0:
            raw_rms.append(0.0)
        else:
            raw_rms.append(float(np.sqrt(np.mean(seg * seg))))
    peak = max(raw_rms) if raw_rms else 0.0
    if peak < 1e-5:
        return [0.0] * len(raw_rms)
    return [min(1.0, v / (peak * 0.85)) for v in raw_rms]


def _synth_with_sapi(text: str, wav_path: Path, speed: float = 1.0) -> tuple[np.ndarray, int]:
    """Synthesize WAV audio via Windows System.Speech on CPU (zero external deps)."""
    clean_text = strip_pause_tags(text)
    sapi_rate = max(-4, min(4, int(round((speed - 1.0) * 6))))
    with tempfile.TemporaryDirectory() as tmp:
        ps_file = Path(tmp) / "sapi_tts.ps1"
        txt_file = Path(tmp) / "input.txt"
        txt_file.write_text(clean_text, encoding="utf-8")
        ps_file.write_text(
            f"""$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = $synth.GetInstalledVoices() | ForEach-Object {{ $_.VoiceInfo.Name }}
if ($voices -contains "Microsoft Zira Desktop") {{
    $synth.SelectVoice("Microsoft Zira Desktop")
}}
$synth.Rate = {sapi_rate}
$text = Get-Content -Raw -Encoding UTF8 "{txt_file}"
$synth.SetOutputToWaveFile("{wav_path}")
$synth.Speak($text)
$synth.SetOutputToNull()
$synth.Dispose()
""",
            encoding="utf-8",
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_file)],
            check=True,
            capture_output=True,
            text=True,
        )

    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
        sampwidth = wf.getsampwidth()
    if sampwidth == 2:
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    else:
        arr = np.zeros(max(1, int(sr * 1.0)), dtype=np.float32)
    return arr, sr


def synthesize_text_to_audio(
    text: str,
    mp3_path: Path,
    voice: str = "af_heart",
    speed: float = 1.0,
    keep_wav_path: Optional[Path] = None,
    models_dir: Path = KOKORO_MODELS_DIR,
    allow_download: bool = False,
) -> dict[str, Any]:
    """Synthesize spoken narration to MP3 (and optional WAV), returning timing & 30fps RMS."""
    mp3_path = Path(mp3_path)
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    speed = max(0.7, min(1.5, float(speed)))
    ffmpeg_exe = find_ffmpeg_executable()

    kokoro = None
    if voice != "sapi_zira":
        try:
            kokoro = _get_kokoro_instance(models_dir=models_dir, allow_download=allow_download)
        except Exception:
            kokoro = None

    with tempfile.TemporaryDirectory() as tmp:
        tmp_wav = keep_wav_path or (Path(tmp) / "speech.wav")
        tmp_wav.parent.mkdir(parents=True, exist_ok=True)

        if kokoro is not None and sf is not None:
            lang = "en-gb" if voice.startswith("b") else "en-us"
            pieces: list[np.ndarray] = []
            sr = 24000
            for kind, val in parse_pause_segments(text):
                if kind == "pause":
                    pieces.append(np.zeros(int(float(val) * sr), dtype=np.float32))
                else:
                    samples, out_sr = kokoro.create(str(val), voice=voice, speed=speed, lang=lang)
                    sr = int(out_sr)
                    pieces.append(np.asarray(samples, dtype=np.float32))
            combined = np.concatenate(pieces) if pieces else np.zeros(sr, dtype=np.float32)
            sf.write(str(tmp_wav), combined, sr)
            engine_used = "kokoro"
        else:
            combined, sr = _synth_with_sapi(text, tmp_wav, speed=speed)
            engine_used = "sapi"

        subprocess.run(
            [
                str(ffmpeg_exe),
                "-y",
                "-i",
                str(tmp_wav),
                "-c:a",
                "libmp3lame",
                "-q:a",
                "4",
                str(mp3_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    duration_sec = len(combined) / float(sr) if sr > 0 else 1.0
    envelope = compute_envelope_30fps(combined, sr)
    return {
        "engine": engine_used,
        "voice": voice if engine_used == "kokoro" else "sapi_zira",
        "duration_sec": round(duration_sec, 3),
        "sample_rate": sr,
        "mp3_path": str(mp3_path),
        "wav_path": str(keep_wav_path) if keep_wav_path else "",
        "envelope_30fps": envelope,
    }


def _format_srt_timestamp(seconds: float) -> str:
    ms_total = max(0, int(round(seconds * 1000)))
    hrs, rem = divmod(ms_total, 3_600_000)
    mins, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"


def _wrap_caption_chunks(text: str, max_chars: int = 84) -> list[str]:
    clean = strip_pause_tags(text)
    if len(clean) <= max_chars:
        return [clean]
    words = clean.split()
    chunks: list[str] = []
    cur: list[str] = []
    for w in words:
        if sum(len(x) + 1 for x in cur) + len(w) > max_chars and cur:
            chunks.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def build_srt_subtitles(steps_with_timings: list[dict[str, Any]]) -> str:
    """Generate SubRip (.srt) captions for all steps from per-step durations."""
    blocks: list[str] = []
    cue_idx = 1
    for item in steps_with_timings:
        start = float(item["start_sec"])
        dur = max(0.5, float(item["duration_sec"]))
        chunks = _wrap_caption_chunks(str(item.get("text") or ""))
        total_chars = max(1, sum(len(c) for c in chunks))
        cursor = start
        for chunk in chunks:
            slice_dur = dur * (len(chunk) / total_chars)
            end = min(start + dur, cursor + max(0.4, slice_dur))
            blocks.append(
                f"{cue_idx}\n"
                f"{_format_srt_timestamp(cursor)} --> {_format_srt_timestamp(end)}\n"
                f"{chunk}\n"
            )
            cue_idx += 1
            cursor = end
    return "\n".join(blocks)
