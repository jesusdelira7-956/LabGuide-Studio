from __future__ import annotations

import io
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None  # type: ignore[assignment]

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
import session_manager  # noqa: E402


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
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


def extract_text_from_upload(filename: str, raw_bytes: bytes) -> str:
    """Extract plain text from .md, .txt, .docx, or .pdf files."""
    ext = Path(filename or "sop.txt").suffix.lower()
    if ext in {".md", ".txt", ""}:
        return raw_bytes.decode("utf-8", errors="replace")

    if ext == ".docx":
        with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
            xml_bytes = zf.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs: list[str] = []
        for p in root.findall(".//w:p", ns):
            texts = [t.text for t in p.findall(".//w:t", ns) if t.text]
            line = "".join(texts).strip()
            if line:
                paragraphs.append(line)
        return "\n\n".join(paragraphs)

    if ext == ".pdf":
        if PdfReader is None:
            raise RuntimeError("pypdf is required to parse PDF files.")
        reader = PdfReader(io.BytesIO(raw_bytes))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        return "\n\n".join(p for p in pages if p)

    raise ValueError(f"Unsupported SOP document format '{ext}'. Use .md, .txt, .docx, or .pdf.")


def _fallback_parse_blocks(raw_text: str, title_hint: str) -> dict[str, Any]:
    lines = [ln.rstrip() for ln in raw_text.splitlines()]
    doc_title = title_hint.strip() or "Electronic Standard Operating Procedure"
    steps: list[dict[str, Any]] = []

    current_title = ""
    current_body: list[str] = []

    def flush() -> None:
        nonlocal current_title, current_body
        body_text = " ".join(x.strip() for x in current_body if x.strip()).strip()
        if not current_title and not body_text:
            return
        idx = len(steps) + 1
        title = current_title or f"Step {idx}"
        bullets = [
            re.sub(r"^[-*•]\s*", "", b).strip()
            for b in current_body
            if b.strip()
        ][:4]
        if not bullets and body_text:
            bullets = [body_text[:110]]
        steps.append(
            {
                "step_num": idx,
                "title": title[:72],
                "bullets": bullets,
                "instruction": body_text or title,
                "narration": f"Step {idx}: {title}. {body_text}" if body_text else title,
                "note": "",
            }
        )
        current_title = ""
        current_body = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# ") and not steps and not current_title and not current_body:
            if title_hint in {"", "Electronic SOP"}:
                doc_title = stripped[2:].strip()
            continue
        if re.match(r"^#{2,3}\s+", stripped):
            flush()
            current_title = re.sub(r"^#{2,3}\s+", "", stripped)
            continue
        num_match = re.match(r"^(?:Step\s+)?(\d+)[\.\):]\s+(.+)$", stripped, re.IGNORECASE)
        if num_match:
            flush()
            headline = num_match.group(2).strip()
            current_title = headline[:64]
            current_body = [headline]
            continue
        current_body.append(stripped)

    flush()

    if not steps:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw_text) if p.strip()]
        for idx, para in enumerate(paragraphs[:15], start=1):
            clean = re.sub(r"\s+", " ", para)
            steps.append(
                {
                    "step_num": idx,
                    "title": f"Execute Step {idx}",
                    "bullets": [clean[:110]],
                    "instruction": clean,
                    "narration": f"Step {idx}. {clean}",
                    "note": "",
                }
            )

    return {
        "title": doc_title,
        "doc_id": f"SOP-{datetime.now().strftime('%Y%m%d')}",
        "prerequisites": "Verify workspace access and required permissions before starting.",
        "safety_warnings": "",
        "steps": steps,
    }


def parse_sop_into_blocks(
    raw_text: str,
    title_hint: str = "Electronic SOP",
    use_ollama: bool = True,
    model: str = "gemma4:e4b",
    ollama_url: str = "http://localhost:11434",
) -> dict[str, Any]:
    """Split raw SOP text into structured SOP blocks using Ollama (with deterministic fallback)."""
    fallback = _fallback_parse_blocks(raw_text, title_hint)
    if not use_ollama or not raw_text.strip():
        return fallback

    prompt = (
        "You are an enterprise SOP technical writer. Convert the source SOP text into clean JSON "
        "for a step-by-step video & guide.\n"
        "Return ONLY valid JSON matching:\n"
        "{\n"
        '  "title": "Concise SOP title",\n'
        '  "doc_id": "SOP-YYYYMMDD-01",\n'
        '  "prerequisites": "Short prerequisites string or empty",\n'
        '  "safety_warnings": "Short safety/security warning or empty",\n'
        '  "steps": [\n'
        "    {\n"
        '      "step_num": 1,\n'
        '      "title": "3-6 word imperative title",\n'
        '      "bullets": ["short slide bullet 1", "short slide bullet 2"],\n'
        '      "instruction": "1-2 clear imperative sentences",\n'
        '      "narration": "Natural spoken voiceover script for this step",\n'
        '      "note": "Optional brief editorial comment or empty"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        f"Source SOP Text:\n{raw_text[:12000]}"
    )
    payload = {
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "keep_alive": "0",
        "options": {"temperature": 0.2},
    }
    try:
        req = urllib.request.Request(
            f"{ollama_url.rstrip('/')}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        parsed = json.loads(body.get("response", "{}"))
        raw_steps = parsed.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return fallback
        clean_steps: list[dict[str, Any]] = []
        for idx, item in enumerate(raw_steps, start=1):
            title = str(item.get("title") or f"Step {idx}").strip()
            instr = str(item.get("instruction") or title).strip()
            narr = str(item.get("narration") or f"Step {idx}: {title}. {instr}").strip()
            bullets = [str(b).strip() for b in (item.get("bullets") or []) if str(b).strip()][:4]
            if not bullets:
                bullets = [instr[:110]]
            clean_steps.append(
                {
                    "step_num": idx,
                    "title": title,
                    "bullets": bullets,
                    "instruction": instr,
                    "narration": narr,
                    "note": str(item.get("note") or "").strip(),
                }
            )
        return {
            "title": str(parsed.get("title") or fallback["title"]).strip(),
            "doc_id": str(parsed.get("doc_id") or fallback["doc_id"]).strip(),
            "prerequisites": str(parsed.get("prerequisites") or "").strip(),
            "safety_warnings": str(parsed.get("safety_warnings") or "").strip(),
            "steps": clean_steps,
        }
    except Exception:
        return fallback


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: Any, max_width: int) -> list[str]:
    words = (text or "").split()
    if not words:
        return []
    lines: list[str] = []
    cur: list[str] = []
    for word in words:
        candidate = " ".join([*cur, word])
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width or not cur:
            cur.append(word)
        else:
            lines.append(" ".join(cur))
            cur = [word]
    if cur:
        lines.append(" ".join(cur))
    return lines


def render_electronic_slide(
    step_spec: dict[str, Any],
    total_steps: int,
    guide_title: str,
    doc_id: str,
    out_raw: Path,
    out_full: Path,
    out_crop: Path,
    attached_image: Optional[Path] = None,
) -> None:
    """Render a 1920x1080 dark-mode SOP slide (and optional attached screenshot composite)."""
    W, H = 1920, 1080
    img = Image.new("RGB", (W, H), color=(11, 17, 32))
    draw = ImageDraw.Draw(img)

    # Subtle grid accents & main card
    draw.rectangle((0, 0, W, 8), fill=(2, 132, 199))
    draw.rounded_rectangle((72, 64, W - 72, H - 84), radius=18, fill=(19, 28, 49), outline=(44, 59, 94), width=2)

    f_meta = _load_font(22, bold=True)
    f_title = _load_font(46, bold=True)
    f_bullet = _load_font(30, bold=False)
    f_note = _load_font(24, bold=False)

    step_num = int(step_spec["step_num"])
    # Header pills
    draw.rounded_rectangle((112, 98, 340, 146), radius=24, fill=(2, 132, 199))
    draw.text((138, 109), f"STEP {step_num:02d} / {max(1, total_steps):02d}", fill=(255, 255, 255), font=f_meta)

    draw.rounded_rectangle((360, 98, 630, 146), radius=24, fill=(15, 23, 42), outline=(56, 189, 248), width=2)
    draw.text((384, 109), doc_id[:20], fill=(125, 211, 252), font=f_meta)

    draw.text((660, 109), guide_title[:56], fill=(148, 163, 184), font=f_meta)

    # Content layout (with or without attached screenshot)
    if attached_image and attached_image.is_file():
        with Image.open(attached_image) as att:
            att_rgb = att.convert("RGB")
        att_rgb.thumbnail((960, 660))
        ax = 112
        ay = 190
        draw.rounded_rectangle(
            (ax - 6, ay - 6, ax + att_rgb.width + 6, ay + att_rgb.height + 6),
            radius=12,
            fill=(7, 11, 20),
            outline=(56, 189, 248),
            width=2,
        )
        img.paste(att_rgb, (ax, ay))
        text_left = ax + att_rgb.width + 48
        text_width = (W - 112) - text_left
    else:
        text_left = 116
        text_width = W - 232

    y_cursor = 195
    for line in _wrap_text(draw, str(step_spec.get("title") or ""), f_title, text_width)[:2]:
        draw.text((text_left, y_cursor), line, fill=(248, 250, 252), font=f_title)
        y_cursor += 60

    y_cursor += 24
    draw.line((text_left, y_cursor, text_left + min(420, text_width), y_cursor), fill=(56, 189, 248), width=4)
    y_cursor += 34

    bullets = step_spec.get("bullets") or [step_spec.get("instruction") or ""]
    for b_idx, bullet in enumerate(bullets[:4], start=1):
        wrapped = _wrap_text(draw, str(bullet), f_bullet, text_width - 56)[:3]
        if not wrapped:
            continue
        draw.ellipse((text_left, y_cursor + 6, text_left + 32, y_cursor + 38), fill=(14, 165, 233))
        draw.text((text_left + 10, y_cursor + 7), str(b_idx), fill=(255, 255, 255), font=f_meta)
        for wline in wrapped:
            draw.text((text_left + 48, y_cursor), wline, fill=(226, 232, 240), font=f_bullet)
            y_cursor += 42
        y_cursor += 16

    note_text = str(step_spec.get("note") or "").strip()
    if note_text and y_cursor < H - 190:
        ny = max(y_cursor + 10, H - 210)
        draw.rounded_rectangle((text_left, ny, W - 112, ny + 88), radius=10, fill=(15, 23, 42), outline=(56, 189, 248), width=2)
        for idx, nline in enumerate(_wrap_text(draw, f"Note: {note_text}", f_note, text_width - 40)[:2]):
            draw.text((text_left + 20, ny + 14 + idx * 32), nline, fill=(125, 211, 252), font=f_note)

    # Brand badge & footer + bottom progress bar
    if brand.LOGO_PNG_PATH.is_file():
        with Image.open(brand.LOGO_PNG_PATH) as raw_logo:
            emblem = raw_logo.convert("RGBA").resize((62, 62), Image.Resampling.LANCZOS)
        img.paste(emblem, (W - 144, 86), emblem)
    draw.text((112, H - 82), f"{brand.APP_NAME} · {brand.PUBLISHER}", fill=(100, 116, 139), font=f_meta)

    bar_left, bar_right = 72, W - 72
    bar_y = H - 46
    draw.rounded_rectangle((bar_left, bar_y, bar_right, bar_y + 12), radius=6, fill=(30, 41, 59))
    frac = min(1.0, step_num / float(max(1, total_steps)))
    fill_w = max(24, int((bar_right - bar_left) * frac))
    draw.rounded_rectangle((bar_left, bar_y, bar_left + fill_w, bar_y + 12), radius=6, fill=(56, 189, 248))

    out_raw.parent.mkdir(parents=True, exist_ok=True)
    out_full.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_raw, "PNG")
    img.save(out_full, "PNG", optimize=True)
    crop = img.resize((1280, 720), Image.Resampling.LANCZOS)
    crop.save(out_crop, "PNG", optimize=True)


def import_electronic_sop(
    raw_text: str,
    title: str = "Electronic SOP",
    use_ollama: bool = True,
    model: str = "gemma4:e4b",
    ollama_url: str = "http://localhost:11434",
    doc_id: str = "",
    owner: str = brand.PUBLISHER,
) -> dict[str, Any]:
    """Import raw or extracted SOP text into a staging session with 1080p visual slides."""
    session_manager.ensure_directories()
    parsed = parse_sop_into_blocks(
        raw_text=raw_text,
        title_hint=title,
        use_ollama=use_ollama,
        model=model,
        ollama_url=ollama_url,
    )
    final_title = title.strip() if (title.strip() and title.strip() != "Electronic SOP") else parsed["title"]
    final_doc_id = doc_id.strip() or parsed.get("doc_id") or f"SOP-{datetime.now().strftime('%Y%m%d')}"

    session_dir = session_manager.STAGING_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_electronic"
    engine = labguide_engine.LabGuideEngine(
        session_dir=session_dir,
        title=final_title,
        ollama_url=ollama_url,
        model=model,
        redact_pii=False,
        auto_compile=False,
        session_status="staging",
    )

    with labguide_engine.connect_sqlite(engine.db_path) as conn:
        iso_pairs = {
            "source_type": "electronic",
            "doc_id": final_doc_id,
            "version": "1.0",
            "owner": owner.strip() or brand.PUBLISHER,
            "effective_date": datetime.now().strftime("%Y-%m-%d"),
            "prerequisites": parsed.get("prerequisites", ""),
            "safety_warnings": parsed.get("safety_warnings", ""),
        }
        for k, v in iso_pairs.items():
            conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (k, v))

        steps = parsed["steps"]
        total = len(steps)
        for spec in steps:
            num = int(spec["step_num"])
            raw_rel = f"assets/raw/step_{num:03d}_raw.png"
            full_rel = f"assets/step_{num:03d}_full.png"
            crop_rel = f"assets/step_{num:03d}_crop.png"

            render_electronic_slide(
                step_spec=spec,
                total_steps=total,
                guide_title=final_title,
                doc_id=final_doc_id,
                out_raw=session_dir / raw_rel,
                out_full=session_dir / full_rel,
                out_crop=session_dir / crop_rel,
            )

            uia_meta = {
                "source": "electronic_slide",
                "bullets": spec.get("bullets", []),
                "narration_script": spec.get("narration", spec.get("instruction", "")),
                "monitor": {"left": 0, "top": 0, "width": 1920, "height": 1080},
            }
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
                    "slide",
                    960,
                    540,
                    final_title,
                    spec["title"],
                    "Slide",
                    json.dumps(uia_meta),
                    full_rel,
                    crop_rel,
                    spec["title"],
                    spec["instruction"],
                    spec.get("note", ""),
                ),
            )

    engine.compile_guide(use_ollama=False)
    return session_manager.get_session_detail(session_dir)
