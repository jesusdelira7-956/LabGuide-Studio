from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

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

import exporter  # noqa: E402
import labguide_engine  # noqa: E402
import session_manager  # noqa: E402
import tts_engine  # noqa: E402

SOP_DIR = session_manager.WORKFLOWS_DIR / "00_how_to_use_labguide_studio"
DEMO_STAGING_DIR = session_manager.STAGING_DIR / "_demo_for_sop"


def find_edge_executable() -> Path:
    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    which_edge = shutil.which("msedge")
    if which_edge:
        return Path(which_edge)
    raise FileNotFoundError("Microsoft Edge (msedge.exe) not found.")


def find_ffmpeg_executable() -> Path:
    return tts_engine.find_ffmpeg_executable()


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    win_fonts = Path(r"C:\Windows\Fonts")
    names = ["segoeuib.ttf", "arialbd.ttf"] if bold else ["segoeui.ttf", "arial.ttf"]
    for name in names:
        fp = win_fonts / name
        if fp.is_file():
            try:
                return ImageFont.truetype(str(fp), size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def create_demo_staging_session() -> Path:
    """Create a realistic sample staging draft so Review & Blur screenshots show live data."""
    if DEMO_STAGING_DIR.exists():
        shutil.rmtree(DEMO_STAGING_DIR)

    engine = labguide_engine.LabGuideEngine(
        session_dir=DEMO_STAGING_DIR,
        title="Configure Internal API Access",
        redact_pii=False,
        auto_compile=False,
        session_status="staging",
    )

    font_title = load_font(22, bold=True)
    font_body = load_font(16, bold=False)

    # Synthetic UI frame showing sample text & secret token for redaction demo
    for step_num, (heading, line1, line2, circle_xy) in enumerate(
        [
            (
                "Internal Service Portal",
                "Customer: Acme Logistics (PROJ-9921)",
                "API Token: internal_secret_alpha_7741",
                (170, 145),
            ),
            (
                "Internal Service Portal",
                "Endpoint: https://intranet.local/v2",
                "Clicked: Save Configuration",
                (220, 260),
            ),
        ],
        start=1,
    ):
        img = Image.new("RGB", (680, 380), color=(18, 26, 44))
        draw = ImageDraw.Draw(img)
        draw.rectangle((20, 20, 660, 360), outline=(56, 189, 248), width=2)
        draw.text((40, 40), heading, fill=(248, 250, 252), font=font_title)
        draw.rectangle((40, 95, 620, 145), fill=(15, 23, 42), outline=(71, 85, 105), width=1)
        draw.text((54, 110), line1, fill=(226, 232, 240), font=font_body)
        draw.rectangle((40, 165, 620, 215), fill=(15, 23, 42), outline=(71, 85, 105), width=1)
        draw.text((54, 180), line2, fill=(252, 165, 165), font=font_body)
        draw.rectangle((40, 240, 260, 285), fill=(2, 132, 199))
        draw.text((70, 252), "Save Configuration", fill=(255, 255, 255), font=font_body)
        img.save(DEMO_STAGING_DIR / f"assets/raw/step_{step_num:03d}_raw.png", "PNG")
        img.save(DEMO_STAGING_DIR / f"assets/step_{step_num:03d}_full.png", "PNG")

        cx, cy = circle_xy
        draw.ellipse((cx - 18, cy - 18, cx + 18, cy + 18), outline="#FF3B30", width=3)
        img.save(DEMO_STAGING_DIR / f"assets/step_{step_num:03d}_crop.png", "PNG")

    with labguide_engine.connect_sqlite(engine.db_path) as conn:
        conn.execute(
            """
            INSERT INTO steps (
                step_num, timestamp, event_type, x, y,
                window_title, control_name, control_type, uia_json,
                full_image, crop_image, step_title, step_instruction, step_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1000.0,
                "click",
                170,
                145,
                "Internal Service Portal",
                "Customer Field",
                "Edit",
                json.dumps({"monitor": {"left": 0, "top": 0}}),
                "assets/step_001_full.png",
                "assets/step_001_crop.png",
                "Select Customer Project Field",
                "In Internal Service Portal, click the Customer Field edit box.",
                "Verify customer ID before saving.",
            ),
        )
        conn.execute(
            """
            INSERT INTO steps (
                step_num, timestamp, event_type, x, y,
                window_title, control_name, control_type, uia_json,
                full_image, crop_image, step_title, step_instruction, step_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                2,
                1002.0,
                "click",
                220,
                260,
                "Internal Service Portal",
                "Save Configuration",
                "Button",
                json.dumps({"monitor": {"left": 0, "top": 0}}),
                "assets/step_002_full.png",
                "assets/step_002_crop.png",
                "Click Save Configuration",
                "In Internal Service Portal, click the Save Configuration button.",
                "",
            ),
        )
    engine.compile_guide(use_ollama=False)
    return DEMO_STAGING_DIR


def capture_edge_screenshot(edge_exe: Path, url: str, out_png: Path) -> None:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as profile_dir:
        cmd = [
            str(edge_exe),
            "--headless=new",
            "--disable-gpu",
            f"--user-data-dir={profile_dir}",
            "--window-size=1440,900",
            "--virtual-time-budget=4000",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            f"--screenshot={out_png}",
            url,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if not out_png.is_file() or out_png.stat().st_size == 0:
            raise RuntimeError(
                f"Edge screenshot failed for {url} (rc={res.returncode}): {res.stderr}"
            )


def draw_callouts(
    raw_png: Path,
    full_out: Path,
    crop_out: Path,
    crop_box: tuple[int, int, int, int],
    callouts: list[dict[str, Any]],
    draw_mini_hud_overlay: bool = False,
) -> None:
    """Draw numbered badges, highlight boxes, and tight crop from untouched raw PNG."""
    with Image.open(raw_png) as opened:
        img = opened.convert("RGB")

    if draw_mini_hud_overlay:
        # Composite realistic floating Mini-HUD pill & hotkey callout on Step 4
        d_hud = ImageDraw.Draw(img)
        hx1, hy1, hx2, hy2 = 985, 22, 1390, 74
        d_hud.rounded_rectangle((hx1, hy1, hx2, hy2), radius=8, fill=(15, 23, 42), outline=(56, 189, 248), width=2)
        f_bold = load_font(15, bold=True)
        f_reg = load_font(14, bold=False)
        d_hud.text((hx1 + 14, hy1 + 15), "● REC", fill=(239, 68, 68), font=f_bold)
        d_hud.text((hx1 + 80, hy1 + 15), "3 steps", fill=(226, 232, 240), font=f_reg)
        d_hud.rounded_rectangle((hx1 + 165, hy1 + 10, hx1 + 260, hy1 + 40), radius=5, fill=(30, 41, 59))
        d_hud.text((hx1 + 188, hy1 + 15), "Pause", fill=(248, 250, 252), font=f_bold)
        d_hud.rounded_rectangle((hx1 + 272, hy1 + 10, hx1 + 365, hy1 + 40), radius=5, fill=(185, 28, 28))
        d_hud.text((hx1 + 298, hy1 + 15), "Stop", fill=(255, 255, 255), font=f_bold)

    draw = ImageDraw.Draw(img)
    f_badge = load_font(15, bold=True)
    f_label = load_font(14, bold=True)

    for idx, c in enumerate(callouts, start=1):
        box = c.get("box")
        if box:
            draw.rounded_rectangle(box, radius=8, outline="#38BDF8", width=3)
        px, py = c["point"]
        draw.ellipse((px - 18, py - 18, px + 18, py + 18), outline="#FF3B30", width=3)

        bx, by = c.get("badge", (px - 14, py - 34))
        draw.ellipse((bx, by, bx + 26, by + 26), fill="#0284C7", outline="#FFFFFF", width=2)
        draw.text((bx + 8, by + 3), str(idx), fill="#FFFFFF", font=f_badge)

        label = c.get("label", "")
        if label:
            lx, ly = bx + 32, by + 2
            bbox = draw.textbbox((lx, ly), label, font=f_label)
            draw.rounded_rectangle(
                (bbox[0] - 6, bbox[1] - 3, bbox[2] + 6, bbox[3] + 4),
                radius=6,
                fill=(15, 23, 42),
                outline=(56, 189, 248),
                width=1,
            )
            draw.text((lx, ly), label, fill="#F8FAFC", font=f_label)

    img.save(full_out, "PNG", optimize=True)

    cl, ct, cr, cb = crop_box
    cl = max(0, cl)
    ct = max(0, ct)
    cr = min(img.width, cr)
    cb = min(img.height, cb)
    crop = img.crop((cl, ct, cr, cb))
    crop.save(crop_out, "PNG", optimize=True)


def synthesize_voiceovers(
    steps: list[dict[str, Any]],
    audio_dir: Path,
    ffmpeg_exe: Path,
) -> list[float]:
    """Synthesize CPU voiceovers via Windows System.Speech and encode to MP3 via FFmpeg."""
    audio_dir.mkdir(parents=True, exist_ok=True)
    durations: list[float] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        manifest = []
        for s in steps:
            wav_path = tmp_dir / f"step_{s['step_num']:03d}.wav"
            manifest.append({"wav": str(wav_path), "text": s["voice_script"]})

        manifest_json = tmp_dir / "tts_manifest.json"
        manifest_json.write_text(json.dumps(manifest), encoding="utf-8")

        ps_script = tmp_dir / "synth.ps1"
        ps_script.write_text(
            f"""$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = $synth.GetInstalledVoices() | ForEach-Object {{ $_.VoiceInfo.Name }}
if ($voices -contains "Microsoft Zira Desktop") {{
    $synth.SelectVoice("Microsoft Zira Desktop")
}}
$synth.Rate = 1
$items = Get-Content -Raw -Encoding UTF8 "{manifest_json}" | ConvertFrom-Json
foreach ($item in $items) {{
    $synth.SetOutputToWaveFile($item.wav)
    $synth.Speak($item.text)
    $synth.SetOutputToNull()
}}
$synth.Dispose()
""",
            encoding="utf-8",
        )

        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_script)],
            check=True,
            capture_output=True,
            text=True,
        )

        for s in steps:
            wav_path = tmp_dir / f"step_{s['step_num']:03d}.wav"
            mp3_path = audio_dir / f"step_{s['step_num']:03d}.mp3"
            with wave.open(str(wav_path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                dur = frames / float(rate)
            durations.append(dur)

            subprocess.run(
                [
                    str(ffmpeg_exe),
                    "-y",
                    "-i",
                    str(wav_path),
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

    return durations


def render_walkthrough_mp4(
    steps: list[dict[str, Any]],
    durations: list[float],
    sop_dir: Path,
    ffmpeg_exe: Path,
) -> Path:
    """Render CPU-only 1440x900 narrated MP4 walkthrough (0 MB GPU VRAM)."""
    out_mp4 = sop_dir / "walkthrough.mp4"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        seg_paths: list[Path] = []
        for s, dur in zip(steps, durations):
            num = s["step_num"]
            img_path = sop_dir / f"assets/step_{num:03d}_full.png"
            mp3_path = sop_dir / f"assets/audio/step_{num:03d}.mp3"
            seg_path = tmp_dir / f"seg_{num:03d}.mp4"
            total_dur = round(dur + 0.8, 2)

            subprocess.run(
                [
                    str(ffmpeg_exe),
                    "-y",
                    "-loop",
                    "1",
                    "-i",
                    str(img_path),
                    "-i",
                    str(mp3_path),
                    "-filter_complex",
                    "[1:a]apad[aout]",
                    "-map",
                    "0:v",
                    "-map",
                    "[aout]",
                    "-c:v",
                    "libx264",
                    "-tune",
                    "stillimage",
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
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            seg_paths.append(seg_path)

        concat_file = tmp_dir / "concat.txt"
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
    return out_mp4


def build_sop(base_url: str = "http://127.0.0.1:8897") -> Path:
    edge_exe = find_edge_executable()
    ffmpeg_exe = find_ffmpeg_executable()
    session_manager.ensure_directories()

    if SOP_DIR.exists():
        shutil.rmtree(SOP_DIR)

    engine = labguide_engine.LabGuideEngine(
        session_dir=SOP_DIR,
        title="Official Guide: How to Use LabGuide Studio (Jesse in the Lab)",
        model="gemma4:e4b",
        redact_pii=False,
        auto_compile=False,
        session_status="saved",
    )
    with labguide_engine.connect_sqlite(engine.db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("saved_at", datetime.now().isoformat()),
        )

    steps_spec: list[dict[str, Any]] = [
        {
            "step_num": 1,
            "window_title": "LabGuide Studio — System Readiness",
            "control_name": "System Readiness Tab",
            "control_type": "TabItem",
            "step_title": "Check System Readiness Before Recording",
            "step_instruction": (
                "Open the System Readiness tab (or check the top-right status pills) to verify the Python .venv, "
                "all 6 capture/OCR packages, Per-Monitor V2 DPI awareness, and local Ollama availability."
            ),
            "step_note": (
                "If you are documenting elevated Admin tools like Windows Firewall or Task Manager, launch Studio "
                "from an Administrator PowerShell so Windows UIPI permits UI Automation and mouse hooks."
            ),
            "voice_script": (
                "Step 1: Check System Readiness before recording. Open the System Readiness tab to confirm your "
                "Python virtual environment, capture packages, and local Ollama server are green. If you plan to "
                "record administrator apps like Windows Firewall, launch Studio from an elevated PowerShell."
            ),
            "crop_box": (0, 0, 1440, 740),
            "callouts": [
                {"box": (850, 14, 1415, 62), "point": (1180, 38), "badge": (1060, 68), "label": "Live Ollama & Draft Pills"},
                {"box": (25, 220, 1410, 560), "point": (260, 310), "badge": (60, 232), "label": "Pre-Flight Checks"},
            ],
        },
        {
            "step_num": 2,
            "window_title": "LabGuide Studio — Capture Studio",
            "control_name": "Start Recording Button",
            "control_type": "Button",
            "step_title": "Configure Title, Countdown & Instant Draft",
            "step_instruction": (
                "In Capture Studio, enter a Guide Title (or leave default to auto-name from your first target app), "
                "keep the 3-Second Countdown and 'Instant Draft -> Review First' selected, and click Start Recording."
            ),
            "step_note": (
                "Instant Draft uses 0 MB GPU VRAM when you stop recording so you can prune misclicks in seconds "
                "before spending GPU compute on Gemma 4 vision descriptions."
            ),
            "voice_script": (
                "Step 2: Configure your capture settings. Enter a guide title, or leave the default so LabGuide Studio "
                "names it from your target app automatically. Keep the 3-second countdown and Instant Draft enabled, "
                "then click Start Recording."
            ),
            "crop_box": (0, 120, 1440, 900),
            "callouts": [
                {"box": (42, 330, 690, 415), "point": (200, 385), "badge": (470, 340), "label": "Auto-Names if Left Default"},
                {"box": (42, 605, 690, 700), "point": (180, 665), "badge": (385, 612), "label": "0 MB VRAM Instant Draft"},
                {"box": (42, 815, 200, 868), "point": (115, 842), "badge": (208, 828), "label": "Start Recording"},
            ],
        },
        {
            "step_num": 3,
            "window_title": "LabGuide Studio — Advanced Options",
            "control_name": "Advanced Capture, Privacy & VRAM Options",
            "control_type": "Group",
            "step_title": "Enable Mini-HUD, Self-Click Filter & Typing Coalescer",
            "step_instruction": (
                "Expand Advanced Capture, Privacy & VRAM Options to confirm the Floating Mini-HUD, Self-Click Filter, "
                "Password-Aware Typing Coalescer, CPU RapidOCR PII Redaction, and keep_alive=\"0\" are checked."
            ),
            "step_note": (
                "Form typing is grouped into one step and password/token/secret fields mask automatically as ••••••••, "
                "while keep_alive=\"0\" frees ~6.13 GB VRAM immediately after Ollama compiles."
            ),
            "voice_script": (
                "Step 3: Review privacy and VRAM guardrails. Expand Advanced Options to verify the floating Mini-HUD, "
                "self-click filter, password-aware typing coalescer, and keep-alive zero are active. Passwords mask "
                "automatically, and VRAM unloads right after compilation."
            ),
            "crop_box": (0, 260, 820, 900),
            "callouts": [
                {"box": (42, 435, 690, 745), "point": (90, 495), "badge": (450, 445), "label": "Mini-HUD + Self-Click Filter"},
                {"box": (42, 550, 690, 620), "point": (90, 578), "badge": (435, 555), "label": "Masks Passwords as ••••••••"},
                {"box": (42, 695, 690, 742), "point": (90, 718), "badge": (450, 698), "label": "Frees VRAM Immediately"},
            ],
        },
        {
            "step_num": 4,
            "window_title": "LabGuide Mini-HUD — Live Recording",
            "control_name": "Live Capture Feed & Hotkeys",
            "control_type": "Pane",
            "step_title": "Record Your Workflow with Hotkeys & Mini-HUD",
            "step_instruction": (
                "Switch to your target Windows app during the countdown. Use the always-on-top Mini-HUD or press "
                "Ctrl+Alt+P before typing secrets, then press Ctrl+Alt+S (or click Stop) when finished."
            ),
            "step_note": (
                "The floating Mini-HUD uses Windows WDA_EXCLUDEFROMCAPTURE so it stays visible to you while remaining "
                "completely invisible in your captured screenshots."
            ),
            "voice_script": (
                "Step 4: Record your workflow live. Switch to your target app during the countdown. The always-on-top "
                "Mini-HUD tracks your step count while staying invisible to screenshots. Press Control Alt P to pause "
                "around passwords, and Control Alt S when finished."
            ),
            "crop_box": (520, 0, 1440, 540),
            "callouts": [
                {"box": (985, 22, 1390, 74), "point": (1180, 48), "badge": (950, 86), "label": "Always-on-Top Mini-HUD (Hidden from Capture)"},
                {"box": (720, 290, 1395, 420), "point": (820, 355), "badge": (1060, 260), "label": "Global Hotkeys: Pause & Stop"},
            ],
        },
        {
            "step_num": 5,
            "window_title": "LabGuide Studio — Review & Step Editor",
            "control_name": "Save vs Discard Decision Banner",
            "control_type": "Group",
            "step_title": "Review Drafts in Staging & Prune Misclicks",
            "step_instruction": (
                "After stopping, inspect your draft inside Review & Step Editor. Use Move Up, Move Down, or Delete "
                "to remove accidental clicks, and edit any Step Title, Instruction, or Tip inline."
            ),
            "step_note": (
                "New recordings stay in sessions/_staging/ until you explicitly approve them—so test captures never "
                "clutter your Saved Workflows library."
            ),
            "voice_script": (
                "Step 5: Review your staging draft and prune misclicks. LabGuide Studio opens the Review and Step Editor "
                "automatically. Reorder or delete stray clicks, edit text inline, and verify every step before saving."
            ),
            "crop_box": (0, 130, 1440, 895),
            "callouts": [
                {"box": (25, 295, 1410, 485), "point": (1180, 445), "badge": (830, 305), "label": "Staging Gate: Save or Discard"},
                {"box": (25, 575, 1410, 880), "point": (1320, 615), "badge": (1020, 588), "label": "Reorder or Delete Misclicks"},
            ],
        },
        {
            "step_num": 6,
            "window_title": "LabGuide Studio — Manual Redaction Studio",
            "control_name": "Redaction Canvas",
            "control_type": "Custom",
            "step_title": "Apply Manual Gaussian Blur or Solid Blackout",
            "step_instruction": (
                "Click Manual Blur / Redact on any step screenshot, pick Gaussian Blur or Solid Blackout, drag "
                "rectangles over sensitive customer names or tokens, and click Apply Redaction."
            ),
            "step_note": (
                "Every original screenshot is preserved untouched inside assets/raw/, so clicking Reset from Raw "
                "lets you undo redactions safely at any time."
            ),
            "voice_script": (
                "Step 6: Redact custom sensitive details. Open Manual Screenshot Redaction, choose Gaussian Blur or "
                "Solid Blackout, and drag boxes over private names or tokens. Originals stay safe in raw assets so you "
                "can reset anytime."
            ),
            "crop_box": (260, 80, 1180, 820),
            "callouts": [
                {"box": (340, 185, 700, 265), "point": (510, 235), "badge": (715, 195), "label": "Choose Blur or Solid Blackout"},
                {"box": (375, 290, 1065, 675), "point": (600, 460), "badge": (780, 320), "label": "Drag Boxes Over Secrets"},
                {"box": (890, 705, 1070, 760), "point": (980, 732), "badge": (750, 712), "label": "Updates PNG & guide.html"},
            ],
        },
        {
            "step_num": 7,
            "window_title": "LabGuide Studio — Local Ollama VLM Rewrite",
            "control_name": "Run AI Descriptions on All Steps",
            "control_type": "Button",
            "step_title": "Run Gemma 4 Vision Rewrite on Surviving Steps",
            "step_instruction": (
                "Once your step list is clean, click Run AI Descriptions on All Steps (or AI Rewrite Step on a single "
                "card) to generate polished imperative instructions locally with gemma4:e4b."
            ),
            "step_note": (
                "Want a different tone? Switch AI Writing Style between Concise SOP, Detailed Tutorial, and "
                "Troubleshooting Runbook in Capture Studio before clicking AI Rewrite."
            ),
            "voice_script": (
                "Step 7: Generate AI descriptions on your surviving steps. Click Run AI Descriptions on All Steps, or "
                "rewrite a single card. Gemma 4 analyzes each cropped screenshot locally and frees VRAM immediately after."
            ),
            "crop_box": (0, 130, 1440, 895),
            "callouts": [
                {"box": (495, 195, 755, 245), "point": (625, 220), "badge": (470, 256), "label": "Run Ollama VLM Across All Steps"},
                {"box": (500, 820, 765, 875), "point": (690, 845), "badge": (780, 830), "label": "Or Rewrite One Step"},
            ],
        },
        {
            "step_num": 8,
            "window_title": "LabGuide Studio — Saved Workflows Library",
            "control_name": "Saved Workflows Exports",
            "control_type": "Group",
            "step_title": "Save to Library & Export (HTML, DOCX, PDF, Replay)",
            "step_instruction": (
                "Click Save Workflow to Library to promote your reviewed draft into workflows/, then use 1-click "
                "buttons in Saved Workflows to open HTML, download Word .docx, print PDF, copy Base64 MD, or export replay.py."
            ),
            "step_note": (
                "Use standalone .html for zero-dependency Slack/email sharing, .docx or PDF for corporate SOP sign-off, "
                "and replay.py to re-run recorded UIA steps automatically."
            ),
            "voice_script": (
                "Step 8: Save and share your finished SOP. Promote your draft to Saved Workflows, then export in one "
                "click to standalone HTML, Microsoft Word docx, print-ready PDF, Base64 Markdown, or an automated Python "
                "replay script."
            ),
            "crop_box": (0, 130, 1440, 780),
            "callouts": [
                {"box": (25, 420, 1410, 680), "point": (180, 560), "badge": (410, 430), "label": "1-Click Exports: HTML, .docx, PDF, replay.py"},
            ],
        },
    ]

    try:
        # Capture Steps 1..4 BEFORE creating _demo_for_sop so header shows Drafts: 0 cleanly
        early_urls = {
            1: (f"{base_url}/?tab=readiness", (0, 0, 1440, 740)),
            2: (f"{base_url}/?tab=capture", (0, 120, 1440, 900)),
            3: (f"{base_url}/?tab=capture&advanced=1", (0, 200, 1440, 900)),
            4: (f"{base_url}/?tab=capture", (480, 0, 1440, 560)),
        }
        for num, (url, crop_box) in early_urls.items():
            raw_png = SOP_DIR / f"assets/raw/step_{num:03d}_raw.png"
            full_png = SOP_DIR / f"assets/step_{num:03d}_full.png"
            crop_png = SOP_DIR / f"assets/step_{num:03d}_crop.png"
            capture_edge_screenshot(edge_exe, url, raw_png)
            capture_edge_screenshot(edge_exe, f"{url}&sop_step={num}", full_png)
            with Image.open(full_png) as im:
                cl, ct, cr, cb = crop_box
                im.crop((cl, ct, min(im.width, cr), min(im.height, cb))).save(crop_png, "PNG", optimize=True)

        # Create demo staging draft for Steps 5..7 (Review, Manual Redaction, VLM Rewrite)
        demo_dir = create_demo_staging_session()
        q_demo = quote(str(demo_dir))
        mid_urls = {
            5: (f"{base_url}/?tab=review&session={q_demo}", (0, 140, 1440, 900)),
            6: (f"{base_url}/?tab=review&session={q_demo}&blur_step=1&demo_boxes=1", (280, 70, 1160, 830)),
            7: (f"{base_url}/?tab=review&session={q_demo}", (0, 120, 1440, 900)),
        }
        for num, (url, crop_box) in mid_urls.items():
            raw_png = SOP_DIR / f"assets/raw/step_{num:03d}_raw.png"
            full_png = SOP_DIR / f"assets/step_{num:03d}_full.png"
            crop_png = SOP_DIR / f"assets/step_{num:03d}_crop.png"
            capture_edge_screenshot(edge_exe, url, raw_png)
            capture_edge_screenshot(edge_exe, f"{url}&sop_step={num}", full_png)
            with Image.open(full_png) as im:
                cl, ct, cr, cb = crop_box
                im.crop((cl, ct, min(im.width, cr), min(im.height, cb))).save(crop_png, "PNG", optimize=True)

        # Remove demo staging draft BEFORE Step 8 so Saved Workflows Library is clean
        if DEMO_STAGING_DIR.exists():
            shutil.rmtree(DEMO_STAGING_DIR, ignore_errors=True)

        # Write steps into session.db so Step 8's Saved Workflows screenshot shows this SOP card
        with labguide_engine.connect_sqlite(engine.db_path) as conn:
            for spec in steps_spec:
                num = spec["step_num"]
                conn.execute(
                    """
                    INSERT INTO steps (
                        step_num, timestamp, event_type, x, y,
                        window_title, control_name, control_type, uia_json,
                        full_image, crop_image, step_title, step_instruction, step_note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        num,
                        float(1000 + num),
                        "click",
                        spec["callouts"][0]["point"][0],
                        spec["callouts"][0]["point"][1],
                        spec["window_title"],
                        spec["control_name"],
                        spec["control_type"],
                        json.dumps({"monitor": {"left": 0, "top": 0}}),
                        f"assets/step_{num:03d}_full.png",
                        f"assets/step_{num:03d}_crop.png",
                        spec["step_title"],
                        spec["step_instruction"],
                        spec["step_note"],
                    ),
                )

        # Capture Step 8 (Saved Workflows Library showing 00_how_to_use_labguide_studio!)
        raw_8 = SOP_DIR / "assets/raw/step_008_raw.png"
        full_8 = SOP_DIR / "assets/step_008_full.png"
        crop_8 = SOP_DIR / "assets/step_008_crop.png"
        capture_edge_screenshot(edge_exe, f"{base_url}/?tab=library", raw_8)
        capture_edge_screenshot(edge_exe, f"{base_url}/?tab=library&sop_step=8", full_8)
        with Image.open(full_8) as im:
            im.crop((0, 140, 1440, 860)).save(crop_8, "PNG", optimize=True)

        # Synthesize CPU voiceovers + CPU MP4 video walkthrough
        durations = synthesize_voiceovers(steps_spec, SOP_DIR / "assets/audio", ffmpeg_exe)
        render_walkthrough_mp4(steps_spec, durations, SOP_DIR, ffmpeg_exe)

        # Final compile so guide.md & guide.html embed all 8 audio tracks + walkthrough.mp4!
        engine.compile_guide(use_ollama=False)
        docx_bytes, _ctype, _fname = exporter.export_docx(SOP_DIR)
        (SOP_DIR / "guide.docx").write_bytes(docx_bytes)
    finally:
        if DEMO_STAGING_DIR.exists():
            shutil.rmtree(DEMO_STAGING_DIR, ignore_errors=True)

    print(f"[SOP Builder] Built complete visual & voice SOP at: {SOP_DIR}")
    return SOP_DIR


if __name__ == "__main__":
    build_sop()
