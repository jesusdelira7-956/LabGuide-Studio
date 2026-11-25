from __future__ import annotations

import json
import math
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import brand  # noqa: E402

WORKSPACE_ROOT = brand.get_runtime_root()
_SKILL_SCRIPTS = brand.get_skill_dir() / "scripts"
SCRIPTS_DIR = _SKILL_SCRIPTS if _SKILL_SCRIPTS.is_dir() else PROJECT_DIR
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import labguide_engine  # noqa: E402
import presenter_engine  # noqa: E402
import session_manager  # noqa: E402
import tts_engine  # noqa: E402


def _load_caption_font(size: int = 24) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for fp in (Path(r"C:\Windows\Fonts\segoeuib.ttf"), Path(r"C:\Windows\Fonts\arialbd.ttf")):
        if fp.is_file():
            try:
                return ImageFont.truetype(str(fp), size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def _build_caption_overlay_png(text: str, max_w: int, out_png: Path) -> tuple[int, int]:
    """Render a crisp translucent dark caption bar PNG for CPU FFmpeg overlay."""
    clean = tts_engine.strip_pause_tags(text)
    font = _load_caption_font(22)
    dummy = Image.new("RGBA", (max_w, 120), (0, 0, 0, 0))
    d = ImageDraw.Draw(dummy)

    words = clean.split()
    lines: list[str] = []
    cur: list[str] = []
    for w in words:
        cand = " ".join([*cur, w])
        bbox = d.textbbox((0, 0), cand, font=font)
        if bbox[2] - bbox[0] <= max_w - 48 or not cur:
            cur.append(w)
        else:
            lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    lines = lines[:2]
    if not lines:
        lines = [""]

    h = 24 + len(lines) * 32
    overlay = Image.new("RGBA", (max_w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle((0, 0, max_w - 1, h - 1), radius=12, fill=(11, 17, 32, 220), outline=(56, 189, 248, 220), width=2)
    for idx, line in enumerate(lines):
        od.text((20, 12 + idx * 30), line, fill=(248, 250, 252, 255), font=font)
    overlay.save(out_png, "PNG")
    return max_w, h


def _render_single_segment(
    step: dict[str, Any],
    session_dir: Path,
    seg_path: Path,
    ffmpeg_exe: Path,
    voice: str,
    speed: float,
    presenter_mode: str,
    avatar_id: str,
    consent_confirmed: bool,
    zoom_to_click: bool,
    burn_captions: bool,
) -> dict[str, Any]:
    num = int(step["step_num"])
    uia = json.loads(step.get("uia_json") or "{}")
    narration = (
        str(uia.get("narration_script") or "").strip()
        or f"Step {num}: {step.get('step_title') or ''}. {step.get('step_instruction') or ''} {step.get('step_note') or ''}".strip()
    )

    mp3_path = session_dir / "assets" / "audio" / f"step_{num:03d}.mp3"
    tts_res = tts_engine.synthesize_text_to_audio(
        text=narration,
        mp3_path=mp3_path,
        voice=voice,
        speed=speed,
        allow_download=False,
    )
    dur_audio = float(tts_res["duration_sec"])
    total_dur = max(1.5, round(dur_audio + 0.75, 2))

    full_img_path = session_dir / str(step["full_image"])
    with Image.open(full_img_path) as im:
        w, h = im.size
    # Ensure even H.264 dimensions
    out_w = w - (w % 2)
    out_h = h - (h % 2)
    total_frames = max(30, int(math.ceil(total_dur * 30.0)))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        inputs = ["-loop", "1", "-i", str(full_img_path), "-i", str(mp3_path)]

        # Base video stream: optional smooth zoom-to-click via FFmpeg zoompan
        mon = uia.get("monitor") or {"left": 0, "top": 0}
        rel_x = int(step.get("x") or out_w // 2) - int(mon.get("left", 0))
        rel_y = int(step.get("y") or out_h // 2) - int(mon.get("top", 0))
        nx = max(0.18, min(0.82, rel_x / float(max(1, out_w))))
        ny = max(0.18, min(0.82, rel_y / float(max(1, out_h))))

        if zoom_to_click and step.get("event_type") != "slide":
            base_v = (
                f"[0:v]scale={out_w}:{out_h},"
                f"zoompan=z='min(1.22,1+0.22*on/{total_frames})':"
                f"x='iw*{nx:.3f}-iw/zoom/2':y='ih*{ny:.3f}-ih/zoom/2':"
                f"d={total_frames}:s={out_w}x{out_h}:fps=30[vbase]"
            )
        else:
            base_v = f"[0:v]scale={out_w}:{out_h},fps=30[vbase]"

        filters = [base_v]
        cur_label = "vbase"
        next_input_idx = 2

        if burn_captions:
            cap_png = tmp_dir / "caption.png"
            cap_w = min(out_w - 360, 960) if presenter_mode == "pip" else min(out_w - 80, 1100)
            _build_caption_overlay_png(narration, max(420, cap_w), cap_png)
            inputs.extend(["-loop", "1", "-i", str(cap_png)])
            filters.append(f"[{cur_label}][{next_input_idx}:v]overlay=28:H-h-24:shortest=1[vcap]")
            cur_label = "vcap"
            next_input_idx += 1

        if presenter_mode == "pip":
            pip_mp4 = tmp_dir / "pip.mp4"
            presenter_engine.render_pip_avatar_video(
                avatar_id=avatar_id,
                envelope_30fps=tts_res["envelope_30fps"],
                duration_sec=total_dur,
                out_mp4=pip_mp4,
                ffmpeg_exe=ffmpeg_exe,
                consent_confirmed=consent_confirmed,
            )
            inputs.extend(["-i", str(pip_mp4)])
            filters.append(f"[{cur_label}][{next_input_idx}:v]overlay=W-w-24:H-h-24:shortest=1[vout]")
            cur_label = "vout"
            next_input_idx += 1

        filters.append("[1:a]apad[aout]")
        filter_complex = ";".join(filters)

        seg_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(ffmpeg_exe),
            "-y",
            *inputs,
            "-filter_complex",
            filter_complex,
            "-map",
            f"[{cur_label}]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-t",
            str(total_dur),
            str(seg_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    return {
        "step_num": num,
        "text": narration,
        "duration_sec": total_dur,
        "tts_engine": tts_res["engine"],
        "voice": tts_res["voice"],
    }


def _concat_segments(seg_paths: list[Path], out_mp4: Path, ffmpeg_exe: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        concat_file = Path(tmp) / "concat.txt"
        concat_file.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in seg_paths),
            encoding="utf-8",
        )
        subprocess.run(
            [
                str(ffmpeg_exe),
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-c",
                "copy",
                str(out_mp4),
            ],
            check=True,
            capture_output=True,
            text=True,
        )


def render_session_video_sop(
    session_dir: Path,
    voice: str = "af_heart",
    speed: float = 1.0,
    presenter_mode: str = "none",
    avatar_id: str = "aria_architect",
    consent_confirmed: bool = False,
    zoom_to_click: bool = True,
    burn_captions: bool = True,
) -> dict[str, Any]:
    """Render full CPU-only video SOP (Kokoro TTS + zoom-to-click + optional legal PiP + SRT)."""
    if presenter_mode not in {"none", "pip"}:
        raise ValueError("presenter_mode must be 'none' or 'pip'.")
    if presenter_mode == "pip" and not consent_confirmed:
        raise ValueError(
            "Explicit ownership/consent confirmation is required to render an AI Presenter PiP."
        )

    resolved = session_manager.resolve_safe_session_dir(session_dir)
    ffmpeg_exe = tts_engine.find_ffmpeg_executable()

    with labguide_engine.connect_sqlite(resolved / "session.db") as conn:
        conn.row_factory = sqlite3.Row
        steps = [dict(r) for r in conn.execute("SELECT * FROM steps ORDER BY step_num ASC").fetchall()]
    if not steps:
        raise ValueError("Cannot render video for a session with 0 steps.")

    cache_dir = resolved / ".video_cache"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    timings: list[dict[str, Any]] = []
    seg_paths: list[Path] = []
    cursor_sec = 0.0
    used_engine = "sapi"
    for step in steps:
        num = int(step["step_num"])
        seg_path = cache_dir / f"seg_{num:03d}.mp4"
        info = _render_single_segment(
            step=step,
            session_dir=resolved,
            seg_path=seg_path,
            ffmpeg_exe=ffmpeg_exe,
            voice=voice,
            speed=speed,
            presenter_mode=presenter_mode,
            avatar_id=avatar_id,
            consent_confirmed=consent_confirmed,
            zoom_to_click=zoom_to_click,
            burn_captions=burn_captions,
        )
        used_engine = info["tts_engine"]
        info["start_sec"] = round(cursor_sec, 3)
        cursor_sec += float(info["duration_sec"])
        timings.append(info)
        seg_paths.append(seg_path)

    out_mp4 = resolved / "walkthrough.mp4"
    _concat_segments(seg_paths, out_mp4, ffmpeg_exe)

    srt_text = tts_engine.build_srt_subtitles(timings)
    (resolved / "walkthrough.srt").write_text(srt_text, encoding="utf-8")

    render_meta = {
        "voice": voice,
        "speed": speed,
        "presenter_mode": presenter_mode,
        "avatar_id": avatar_id,
        "consent_confirmed": consent_confirmed,
        "zoom_to_click": zoom_to_click,
        "burn_captions": burn_captions,
        "timings": timings,
    }
    (cache_dir / "render_config.json").write_text(json.dumps(render_meta, indent=2), encoding="utf-8")

    engine = labguide_engine.LabGuideEngine(
        session_dir=resolved,
        title=None,
        redact_pii=False,
        session_status=None,
    )
    engine.compile_guide(use_ollama=False)

    return {
        "detail": session_manager.get_session_detail(resolved),
        "tts_engine": used_engine,
        "duration_sec": round(cursor_sec, 2),
        "video_path": str(out_mp4),
    }


def rerender_single_step(
    session_dir: Path,
    step_num: int,
    voice: Optional[str] = None,
    speed: Optional[float] = None,
    presenter_mode: Optional[str] = None,
    avatar_id: Optional[str] = None,
    consent_confirmed: Optional[bool] = None,
    zoom_to_click: Optional[bool] = None,
    burn_captions: Optional[bool] = None,
) -> dict[str, Any]:
    """Re-render only one changed step segment and re-concatenate walkthrough.mp4."""
    resolved = session_manager.resolve_safe_session_dir(session_dir)
    cache_dir = resolved / ".video_cache"
    cfg_path = cache_dir / "render_config.json"
    if not cfg_path.is_file():
        return render_session_video_sop(
            resolved,
            voice=voice or "af_heart",
            speed=speed or 1.0,
            presenter_mode=presenter_mode or "none",
            avatar_id=avatar_id or "aria_architect",
            consent_confirmed=bool(consent_confirmed),
            zoom_to_click=True if zoom_to_click is None else zoom_to_click,
            burn_captions=True if burn_captions is None else burn_captions,
        )

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    v_voice = voice or cfg.get("voice", "af_heart")
    v_speed = float(speed if speed is not None else cfg.get("speed", 1.0))
    v_mode = presenter_mode or cfg.get("presenter_mode", "none")
    v_avatar = avatar_id or cfg.get("avatar_id", "aria_architect")
    v_consent = bool(consent_confirmed if consent_confirmed is not None else cfg.get("consent_confirmed", False))
    v_zoom = bool(zoom_to_click if zoom_to_click is not None else cfg.get("zoom_to_click", True))
    v_burn = bool(burn_captions if burn_captions is not None else cfg.get("burn_captions", True))

    if v_mode == "pip" and not v_consent:
        raise ValueError("Explicit consent confirmation is required for Presenter PiP.")

    with labguide_engine.connect_sqlite(resolved / "session.db") as conn:
        conn.row_factory = sqlite3.Row
        steps = [dict(r) for r in conn.execute("SELECT * FROM steps ORDER BY step_num ASC").fetchall()]

    target_step = next((s for s in steps if int(s["step_num"]) == int(step_num)), None)
    if not target_step:
        raise ValueError(f"Step {step_num} not found.")

    ffmpeg_exe = tts_engine.find_ffmpeg_executable()
    seg_path = cache_dir / f"seg_{int(step_num):03d}.mp4"
    updated_info = _render_single_segment(
        step=target_step,
        session_dir=resolved,
        seg_path=seg_path,
        ffmpeg_exe=ffmpeg_exe,
        voice=v_voice,
        speed=v_speed,
        presenter_mode=v_mode,
        avatar_id=v_avatar,
        consent_confirmed=v_consent,
        zoom_to_click=v_zoom,
        burn_captions=v_burn,
    )

    old_timings = {int(t["step_num"]): t for t in cfg.get("timings", [])}
    old_timings[int(step_num)] = updated_info

    seg_paths: list[Path] = []
    new_timings: list[dict[str, Any]] = []
    cursor = 0.0
    for s in steps:
        num = int(s["step_num"])
        sp = cache_dir / f"seg_{num:03d}.mp4"
        if not sp.is_file():
            info = _render_single_segment(
                step=s,
                session_dir=resolved,
                seg_path=sp,
                ffmpeg_exe=ffmpeg_exe,
                voice=v_voice,
                speed=v_speed,
                presenter_mode=v_mode,
                avatar_id=v_avatar,
                consent_confirmed=v_consent,
                zoom_to_click=v_zoom,
                burn_captions=v_burn,
            )
        else:
            info = dict(old_timings.get(num) or {"step_num": num, "text": s.get("step_instruction", ""), "duration_sec": 3.0})
        info["start_sec"] = round(cursor, 3)
        cursor += float(info["duration_sec"])
        new_timings.append(info)
        seg_paths.append(sp)

    out_mp4 = resolved / "walkthrough.mp4"
    _concat_segments(seg_paths, out_mp4, ffmpeg_exe)
    (resolved / "walkthrough.srt").write_text(tts_engine.build_srt_subtitles(new_timings), encoding="utf-8")
    cfg["timings"] = new_timings
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    engine = labguide_engine.LabGuideEngine(
        session_dir=resolved,
        title=None,
        redact_pii=False,
        session_status=None,
    )
    engine.compile_guide(use_ollama=False)
    return {
        "detail": session_manager.get_session_detail(resolved),
        "rerendered_step": int(step_num),
        "duration_sec": round(cursor, 2),
    }
