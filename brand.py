from __future__ import annotations

import base64
import io
import math
import shutil
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

APP_NAME = "LabGuide Studio"
APP_SLUG = "labguide_studio"
SKILL_SLUG = "labguide-studio"
APP_VERSION = "3.0.0"
PUBLISHER = "Jesse in the Lab"
COPYRIGHT_NOTICE = "Copyright (c) 2026 Jesse in the Lab"
LICENSE_NAME = "MIT License"
TAGLINE = "100% Local Windows 11 Workflow Recorder, Electronic-to-Video SOP Suite & Ollama VLM Editor"
DEFAULT_PORT = 8897

PROJECT_DIR = Path(__file__).resolve().parent


def is_nested_in_workspace() -> bool:
    """Return True when running nested under <workspace>/projects/labguide_studio with a parent .venv."""
    return (
        PROJECT_DIR.parent.name == "projects"
        and len(PROJECT_DIR.parents) >= 2
        and (PROJECT_DIR.parents[1] / ".venv").is_dir()
    )


def get_runtime_root() -> Path:
    """Return parent workspace root when nested, or PROJECT_DIR in a standalone clone."""
    return PROJECT_DIR.parents[1] if is_nested_in_workspace() else PROJECT_DIR


def get_venv_python() -> Path:
    """Return local standalone .venv python if present, else workspace .venv python, else sys.executable."""
    candidates = [PROJECT_DIR / ".venv" / "Scripts" / "python.exe"]
    if len(PROJECT_DIR.parents) >= 2 and PROJECT_DIR.parent.name == "projects":
        candidates.append(PROJECT_DIR.parents[1] / ".venv" / "Scripts" / "python.exe")
    for cand in candidates:
        if cand.is_file():
            return cand
    return Path(sys.executable)


def get_kokoro_models_dir() -> Path:
    """Resolve Kokoro ONNX weights folder for standalone or nested workspace installs."""
    local_models = PROJECT_DIR / "models" / "kokoro"
    if local_models.is_dir():
        return local_models
    if len(PROJECT_DIR.parents) >= 2:
        ws_models = PROJECT_DIR.parents[1] / "models" / "kokoro"
        if ws_models.is_dir():
            return ws_models
    return local_models


def get_skill_dir() -> Path:
    """Resolve companion Agent Skill folder if present, otherwise PROJECT_DIR."""
    candidates = [PROJECT_DIR / ".agents" / "skills" / SKILL_SLUG]
    if len(PROJECT_DIR.parents) >= 2 and PROJECT_DIR.parent.name == "projects":
        candidates.append(PROJECT_DIR.parents[1] / ".agents" / "skills" / SKILL_SLUG)
    for cand in candidates:
        if cand.is_dir():
            return cand
    return PROJECT_DIR


WORKSPACE_ROOT = get_runtime_root()
ASSETS_DIR = PROJECT_DIR / "assets"
BRAND_DIR = ASSETS_DIR / "brand"
LOGO_PNG_PATH = BRAND_DIR / "jesse_lab_logo.png"
BG_JPG_PATH = BRAND_DIR / "jesse_lab_bg.jpg"
ICO_PATH = ASSETS_DIR / "labguide_studio.ico"
LICENSE_PATH = PROJECT_DIR / "LICENSE"

ICO_SIZES: list[tuple[int, int]] = [
    (16, 16),
    (24, 24),
    (32, 32),
    (48, 48),
    (64, 64),
    (128, 128),
    (256, 256),
]

THIRD_PARTY_ATTRIBUTIONS: list[dict[str, str]] = [
    {"component": "Kokoro-82M ONNX (kokoro-onnx)", "license": "Apache-2.0", "role": "CPU Neural Voice Synthesis"},
    {"component": "Ollama", "license": "MIT", "role": "Local Multimodal Vision-Language Inference"},
    {"component": "FFmpeg", "license": "LGPL / GPL (External CLI)", "role": "Video Zoompan, Subtitles & Audio Muxing"},
    {"component": "RapidOCR ONNX Runtime", "license": "Apache-2.0", "role": "100% Local CPU OCR PII Redaction"},
    {"component": "Pillow (PIL)", "license": "HPND", "role": "Image Annotation, Blur & 1080p Slide Compositing"},
    {"component": "pypdf", "license": "BSD-3-Clause", "role": "Electronic PDF SOP Extraction"},
    {"component": "mss", "license": "MIT", "role": "Multi-Monitor High-DPI Screen Capture"},
    {"component": "pynput", "license": "LGPL-3.0", "role": "Non-Blocking Mouse & Keyboard Listeners"},
    {"component": "uiautomation", "license": "Apache-2.0", "role": "Windows 11 UIA Accessibility Inspection"},
    {"component": "requests", "license": "Apache-2.0", "role": "Localhost Ollama HTTP Client"},
    {"component": "soundfile", "license": "BSD-3-Clause", "role": "WAV Audio Encoding"},
    {"component": "Built-In Studio Presenters", "license": "CC0-1.0 / Original Synthetic Artwork", "role": "Consent-Gated PiP Avatars"},
]


def prepare_circular_logo_from_source(
    source_path: Path,
    png_out: Path = LOGO_PNG_PATH,
    ico_out: Path = ICO_PATH,
) -> tuple[Path, Path]:
    """Crop the circular emblem from source_path, apply smooth alpha masking, and save 512px PNG + multi-res ICO."""
    source = Path(source_path)
    if not source.is_file():
        raise FileNotFoundError(f"Logo source image not found: {source}")

    png_out = Path(png_out)
    ico_out = Path(ico_out)
    png_out.parent.mkdir(parents=True, exist_ok=True)
    ico_out.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source) as raw_im:
        rgba = raw_im.convert("RGBA")
        w, h = rgba.size
        px = rgba.load()

        # Sample outer corner background color so dark square corners are tightly stripped
        samples = [
            px[4, 4][:3],
            px[max(0, w - 5), 4][:3],
            px[4, max(0, h - 5)][:3],
            px[max(0, w - 5), max(0, h - 5)][:3],
        ]
        bg_r = sum(s[0] for s in samples) / 4.0
        bg_g = sum(s[1] for s in samples) / 4.0
        bg_b = sum(s[2] for s in samples) / 4.0

        min_x, min_y = w, h
        max_x, max_y = -1, -1
        thresh_sq = 26.0 * 26.0
        for y in range(h):
            for x in range(w):
                r, g, b, _a = px[x, y]
                dist_sq = (r - bg_r) ** 2 + (g - bg_g) ** 2 + (b - bg_b) ** 2
                if dist_sq > thresh_sq:
                    if x < min_x:
                        min_x = x
                    if y < min_y:
                        min_y = y
                    if x > max_x:
                        max_x = x
                    if y > max_y:
                        max_y = y

        if max_x <= min_x or max_y <= min_y:
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            cropped = rgba.crop((left, top, left + side, top + side))
        else:
            cx = (min_x + max_x) / 2.0
            cy = (min_y + max_y) / 2.0
            span = max(max_x - min_x, max_y - min_y)
            half = max(16.0, span * 0.51)
            box = (
                max(0, int(round(cx - half))),
                max(0, int(round(cy - half))),
                min(w, int(round(cx + half))),
                min(h, int(round(cy + half))),
            )
            cropped = rgba.crop(box)

        # Normalize to crisp 1024x1024 canvas for 2x-supersampled circular alpha mask
        hi_size = 1024
        hi_img = cropped.resize((hi_size, hi_size), Image.Resampling.LANCZOS)
        mask = Image.new("L", (hi_size, hi_size), 0)
        md = ImageDraw.Draw(mask)
        inset = 10
        md.ellipse((inset, inset, hi_size - inset, hi_size - inset), fill=255)

        masked = Image.new("RGBA", (hi_size, hi_size), (0, 0, 0, 0))
        masked.paste(hi_img, (0, 0), mask)

        master_512 = masked.resize((512, 512), Image.Resampling.LANCZOS)
        master_512.save(png_out, format="PNG", optimize=True)

        master_256 = masked.resize((256, 256), Image.Resampling.LANCZOS)
        master_256.save(ico_out, format="ICO", sizes=ICO_SIZES)

    return png_out, ico_out


def install_background_from_source(source_path: Path, bg_out: Path = BG_JPG_PATH) -> Path:
    """Copy or normalize the uploaded Jesse in the Lab background image into brand assets."""
    source = Path(source_path)
    if not source.is_file():
        raise FileNotFoundError(f"Background image not found: {source}")
    bg_out = Path(bg_out)
    bg_out.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as im:
        rgb = im.convert("RGB")
        rgb.save(bg_out, format="JPEG", quality=92, optimize=True)
    return bg_out


def get_logo_data_uri(size: int = 96) -> str:
    """Return a compact base64 PNG data URI of the Jesse in the Lab logo for self-contained HTML exports."""
    if not LOGO_PNG_PATH.is_file():
        return ""
    try:
        with Image.open(LOGO_PNG_PATH) as im:
            thumb = im.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            thumb.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:
        return ""


def read_license_text() -> str:
    if LICENSE_PATH.is_file():
        return LICENSE_PATH.read_text(encoding="utf-8")
    return f"{LICENSE_NAME}\n\n{COPYRIGHT_NOTICE}\n"


def get_about_payload(port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Return complete application provenance, licensing, and privacy metadata for /api/about."""
    return {
        "app_name": APP_NAME,
        "app_slug": APP_SLUG,
        "skill_slug": SKILL_SLUG,
        "version": APP_VERSION,
        "publisher": PUBLISHER,
        "copyright": COPYRIGHT_NOTICE,
        "license": LICENSE_NAME,
        "license_text": read_license_text(),
        "tagline": TAGLINE,
        "port": int(port),
        "platform": sys.platform,
        "python_version": sys.version.split()[0],
        "privacy_guarantee": (
            "100% local execution. Screenshots, UIA metadata, OCR text, Kokoro TTS voices, "
            "avatar videos, and Ollama VLM prompts never leave this Windows 11 workstation."
        ),
        "third_party": THIRD_PARTY_ATTRIBUTIONS,
        "brand_assets": {
            "logo_ready": LOGO_PNG_PATH.is_file(),
            "background_ready": BG_JPG_PATH.is_file(),
            "icon_ready": ICO_PATH.is_file(),
        },
    }
