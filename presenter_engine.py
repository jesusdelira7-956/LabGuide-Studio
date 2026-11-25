from __future__ import annotations

import io
import json
import math
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

PROJECT_DIR = Path(__file__).resolve().parent
AVATARS_DIR = PROJECT_DIR / "assets" / "avatars"
MANIFEST_PATH = AVATARS_DIR / "manifest.json"

BUILTIN_AVATARS = [
    {
        "id": "aria_architect",
        "name": "Aria",
        "role": "Systems Architect (Studio Avatar)",
        "license": "CC0-1.0 / Original Synthetic Artwork",
        "consent_confirmed": True,
        "bg_top": (15, 23, 42),
        "bg_bot": (30, 58, 138),
        "skin": (235, 196, 174),
        "hair": (44, 34, 46),
        "suit": (2, 132, 199),
        "lip": (185, 86, 98),
        "emblem": False,
    },
    {
        "id": "marcus_sre",
        "name": "Marcus",
        "role": "Operations Lead (Studio Avatar)",
        "license": "CC0-1.0 / Original Synthetic Artwork",
        "consent_confirmed": True,
        "bg_top": (15, 23, 42),
        "bg_bot": (17, 94, 89),
        "skin": (184, 130, 98),
        "hair": (28, 28, 34),
        "suit": (51, 65, 85),
        "lip": (146, 78, 72),
        "emblem": False,
    },
    {
        "id": "nova_ai",
        "name": "Nova",
        "role": "Jesse in the Lab Guide Emblem (Studio Avatar)",
        "license": "CC0-1.0 / Original Synthetic Artwork",
        "consent_confirmed": True,
        "bg_top": (11, 17, 32),
        "bg_bot": (30, 41, 59),
        "skin": (56, 189, 248),
        "hair": (14, 165, 233),
        "suit": (15, 23, 42),
        "lip": (224, 242, 254),
        "emblem": True,
    },
]


def _render_builtin_portrait(spec: dict[str, Any], out_path: Path) -> None:
    """Render a crisp 512x512 studio avatar portrait via 2x supersampled Pillow geometry."""
    S = 1024
    img = Image.new("RGB", (S, S), spec["bg_top"])
    draw = ImageDraw.Draw(img)

    # Vertical gradient backdrop
    r1, g1, b1 = spec["bg_top"]
    r2, g2, b2 = spec["bg_bot"]
    for y in range(S):
        t = y / float(S - 1)
        col = (int(r1 + (r2 - r1) * t), int(g1 + (g2 - g1) * t), int(b1 + (b2 - b1) * t))
        draw.line((0, y, S, y), fill=col)

    if spec["emblem"]:
        draw.ellipse((192, 192, 832, 832), fill=(15, 23, 42), outline=(56, 189, 248), width=18)
        draw.ellipse((280, 280, 744, 744), outline=(14, 165, 233), width=8)
        # Eyes / visor indicators
        draw.ellipse((380, 410, 440, 470), fill=(224, 242, 254))
        draw.ellipse((584, 410, 644, 470), fill=(224, 242, 254))
    else:
        # Shoulders / suit
        draw.pieslice((120, 660, 904, 1280), 180, 360, fill=spec["suit"])
        draw.polygon([(430, 670), (594, 670), (512, 840)], fill=(241, 245, 249))
        # Neck
        draw.rectangle((454, 570, 570, 700), fill=spec["skin"])
        # Hair backdrop
        draw.ellipse((308, 175, 716, 610), fill=spec["hair"])
        # Head
        draw.ellipse((336, 224, 688, 624), fill=spec["skin"])
        # Hair cap
        draw.chord((326, 184, 698, 430), 180, 360, fill=spec["hair"])
        # Brows
        draw.line((396, 365, 466, 358), fill=spec["hair"], width=10)
        draw.line((558, 358, 628, 365), fill=spec["hair"], width=10)
        # Eyes
        draw.ellipse((410, 392, 456, 438), fill=(248, 250, 252))
        draw.ellipse((568, 392, 614, 438), fill=(248, 250, 252))
        draw.ellipse((422, 402, 446, 426), fill=(15, 23, 42))
        draw.ellipse((580, 402, 604, 426), fill=(15, 23, 42))
        # Nose subtle shadow
        draw.line((512, 425, 502, 488, 520, 492), fill=(165, 118, 96), width=6)

    out_img = img.resize((512, 512), Image.Resampling.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_img.save(out_path, "PNG")


def ensure_avatars_manifest(avatars_dir: Path = AVATARS_DIR) -> dict[str, Any]:
    """Ensure built-in Studio Presenters and manifest.json provenance ledger exist."""
    avatars_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = avatars_dir / "manifest.json"
    if manifest_file.is_file():
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    else:
        data = {"avatars": []}

    existing_ids = {item["id"]: item for item in data.get("avatars", [])}
    changed = False
    for spec in BUILTIN_AVATARS:
        png_path = avatars_dir / f"{spec['id']}.png"
        if not png_path.is_file():
            _render_builtin_portrait(spec, png_path)
        if spec["id"] not in existing_ids:
            entry = {
                "id": spec["id"],
                "name": spec["name"],
                "role": spec["role"],
                "file": png_path.name,
                "license": spec["license"],
                "consent_confirmed": True,
                "created_at": datetime.now().isoformat(),
                "is_builtin": True,
                "is_emblem": spec["emblem"],
                "skin_rgb": list(spec["skin"]),
                "lip_rgb": list(spec["lip"]),
                "mouth_y": 272 if not spec["emblem"] else 305,
                "eye_y": 208 if not spec["emblem"] else 220,
            }
            data.setdefault("avatars", []).append(entry)
            existing_ids[spec["id"]] = entry
            changed = True

    if changed or not manifest_file.is_file():
        manifest_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def list_avatars(avatars_dir: Path = AVATARS_DIR) -> list[dict[str, Any]]:
    data = ensure_avatars_manifest(avatars_dir)
    return list(data.get("avatars", []))


def register_custom_avatar(
    name: str,
    role_title: str,
    image_bytes: bytes,
    ext: str = ".png",
    consent_confirmed: bool = False,
    avatars_dir: Path = AVATARS_DIR,
) -> dict[str, Any]:
    """Register a user-owned or consented custom avatar after explicit consent verification."""
    if not consent_confirmed:
        raise ValueError(
            "Explicit ownership/consent confirmation is required to register a custom avatar."
        )
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Avatar name is required.")

    ensure_avatars_manifest(avatars_dir)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", clean_name.lower()).strip("_") or "custom"
    avatar_id = f"custom_{slug}_{datetime.now().strftime('%H%M%S')}"
    out_file = avatars_dir / f"{avatar_id}.png"

    with Image.open(io.BytesIO(image_bytes)) as opened:
        rgb = opened.convert("RGB")
        # Center-crop to square and resize to 512x512
        side = min(rgb.width, rgb.height)
        left = (rgb.width - side) // 2
        top = (rgb.height - side) // 2
        cropped = rgb.crop((left, top, left + side, top + side)).resize(
            (512, 512), Image.Resampling.LANCZOS
        )
        cropped.save(out_file, "PNG")

    manifest_file = avatars_dir / "manifest.json"
    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    entry = {
        "id": avatar_id,
        "name": clean_name,
        "role": (role_title or "Custom Studio Presenter").strip(),
        "file": out_file.name,
        "license": "User-Provided (Consent Confirmed)",
        "consent_confirmed": True,
        "created_at": datetime.now().isoformat(),
        "is_builtin": False,
        "is_emblem": False,
        "mouth_y": 310,
        "eye_y": 215,
    }
    data["avatars"].append(entry)
    manifest_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return entry


def render_pip_avatar_video(
    avatar_id: str,
    envelope_30fps: list[float],
    duration_sec: float,
    out_mp4: Path,
    ffmpeg_exe: Path,
    consent_confirmed: bool = False,
    avatars_dir: Path = AVATARS_DIR,
) -> Path:
    """Render a 280x280 30fps CPU audio-reactive PiP presenter clip (0 MB GPU VRAM)."""
    if not consent_confirmed:
        raise ValueError(
            "Explicit ownership/consent confirmation is required before rendering a presenter PiP."
        )

    avatars = {a["id"]: a for a in list_avatars(avatars_dir)}
    spec = avatars.get(avatar_id) or avatars["aria_architect"]
    portrait_path = avatars_dir / spec["file"]
    if not portrait_path.is_file():
        ensure_avatars_manifest(avatars_dir)

    size = 280
    total_frames = max(15, int(math.ceil(duration_sec * 30.0)))

    with Image.open(portrait_path) as opened:
        base = opened.convert("RGB").resize((size, size), Image.Resampling.LANCZOS)

    # Prebuild circular mask
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((10, 10, size - 10, size - 10), fill=255)
    bg_plate = Image.new("RGB", (size, size), (11, 17, 32))

    is_builtin = bool(spec.get("is_builtin", False))
    is_emblem = bool(spec.get("is_emblem", False))
    lip_col = tuple(spec.get("lip_rgb", [185, 86, 98]))
    skin_col = tuple(spec.get("skin_rgb", [235, 196, 174]))

    cmd = [
        str(ffmpeg_exe),
        "-y",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{size}x{size}",
        "-r",
        "30",
        "-i",
        "-",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        str(out_mp4),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None

    try:
        for f_idx in range(total_frames):
            rms = envelope_30fps[f_idx] if f_idx < len(envelope_30fps) else 0.0
            bob_y = int(round(math.sin(f_idx * 0.14) * 2.0))

            frame = bg_plate.copy()
            shifted = Image.new("RGB", (size, size), (15, 23, 42))
            shifted.paste(base, (0, bob_y))
            frame.paste(shifted, (0, 0), mask)
            d = ImageDraw.Draw(frame)

            # Blink every ~3.4s for 4 frames on built-in portraits
            if is_builtin and not is_emblem and (f_idx % 102) in {96, 97, 98}:
                ey = 114 + bob_y
                d.rectangle((108, ey - 8, 128, ey + 8), fill=skin_col)
                d.rectangle((152, ey - 8, 172, ey + 8), fill=skin_col)
                d.line((108, ey, 128, ey), fill=(44, 34, 46), width=3)
                d.line((152, ey, 172, ey), fill=(44, 34, 46), width=3)

            # Audio-reactive lip-sync viseme (built-in portraits) or pulsing wave ring (emblem/custom)
            if is_builtin and not is_emblem:
                mx, my = 140, 148 + bob_y
                open_h = int(round(2 + rms * 11))
                open_w = int(round(13 + (1.0 - rms) * 4))
                if rms < 0.12:
                    d.line((mx - open_w, my, mx + open_w, my), fill=lip_col, width=3)
                else:
                    d.ellipse((mx - open_w, my - open_h, mx + open_w, my + open_h), fill=(42, 18, 24), outline=lip_col, width=2)
            elif is_emblem:
                my = 162 + bob_y
                bars = [-30, -18, -6, 6, 18, 30]
                for b_i, bx in enumerate(bars):
                    bh = int(round(4 + rms * (18 if b_i in {2, 3} else 12)))
                    d.rounded_rectangle((140 + bx - 3, my - bh, 140 + bx + 3, my + bh), radius=3, fill=(56, 189, 248))

            # Speaking ring intensity driven by RMS
            ring_col = (56, 189, 248) if rms > 0.1 else (51, 65, 85)
            ring_w = 5 if rms > 0.25 else 3
            d.ellipse((8, 8, size - 8, size - 8), outline=ring_col, width=ring_w)

            proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()
        stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"FFmpeg PiP avatar render failed (rc={rc}): {stderr}")

    return out_mp4
