from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import brand  # noqa: E402

WORKSPACE_ROOT = brand.get_runtime_root()
SESSIONS_DIR = PROJECT_DIR / "sessions"
STAGING_DIR = SESSIONS_DIR / "_staging"
WORKFLOWS_DIR = PROJECT_DIR / "workflows"
SKILL_DIR = brand.get_skill_dir()
SCRIPTS_DIR = SKILL_DIR / "scripts" if (SKILL_DIR / "scripts").is_dir() else PROJECT_DIR

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import labguide_engine  # noqa: E402

SAVE_PRESETS: dict[str, Path] = {
    "workflows": WORKFLOWS_DIR,
    "directives_sops": WORKSPACE_ROOT / "directives" / "sops",
    "knowledge_guides": WORKSPACE_ROOT / "knowledge" / "guides",
}

VISION_MODEL_HINTS = (
    "gemma4",
    "gemma3",
    "vl",
    "llava",
    "minicpm-v",
    "moondream",
    "pixtral",
    "vision",
)


def ensure_directories() -> None:
    """Ensure core staging and saved workflow directories exist."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)


def _allowed_session_roots() -> tuple[Path, ...]:
    roots = [
        STAGING_DIR.resolve(),
        WORKFLOWS_DIR.resolve(),
        SESSIONS_DIR.resolve(),
    ]
    for path in SAVE_PRESETS.values():
        resolved = path.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def resolve_safe_session_dir(session_path: str | Path, require_db: bool = True) -> Path:
    """Resolve and validate that session_path resides strictly inside an allowlisted folder."""
    if not session_path or not str(session_path).strip():
        raise ValueError("Session path is required.")
    candidate = Path(session_path).resolve()
    roots = _allowed_session_roots()

    # Must be a strict child of one of the allowed roots, never a root itself or _staging container
    if candidate in roots or candidate == STAGING_DIR.resolve():
        raise ValueError("Invalid session folder target.")
    if not any(candidate.is_relative_to(root) for root in roots):
        raise ValueError(f"Access denied outside LabGuide Studio session roots: {candidate}")
    if not candidate.is_dir():
        raise FileNotFoundError(f"Session folder not found: {candidate}")
    if require_db and not (candidate / "session.db").is_file():
        raise FileNotFoundError(f"Missing session.db in: {candidate}")
    return candidate


def resolve_safe_open_target(target_path: str | Path) -> Path:
    """Resolve and validate a folder or file path before opening it in Windows Explorer."""
    if not target_path or not str(target_path).strip():
        raise ValueError("Target path is required.")
    candidate = Path(target_path).resolve()
    allowed_roots = _allowed_session_roots() + (
        PROJECT_DIR.resolve(),
        SKILL_DIR.resolve(),
    )
    if not any(candidate == root or candidate.is_relative_to(root) for root in allowed_roots):
        raise ValueError(f"Access denied for path: {candidate}")

    # Auto-create preset destination folders if user clicks "Open" before first save
    if not candidate.exists() and candidate in allowed_roots:
        candidate.mkdir(parents=True, exist_ok=True)
    if not candidate.exists():
        raise FileNotFoundError(f"Path does not exist: {candidate}")
    return candidate


def open_in_os(target_path: str | Path, reveal: bool = False) -> Path:
    """Open an allowlisted folder or file using Windows Explorer / default OS handler."""
    resolved = resolve_safe_open_target(target_path)
    if sys.platform == "win32":
        if reveal and resolved.is_file():
            subprocess.Popen(["explorer.exe", "/select,", str(resolved)])
        else:
            os.startfile(str(resolved))  # type: ignore[attr-defined]
    return resolved


def _classify_category(session_dir: Path) -> str:
    resolved = session_dir.resolve()
    if resolved.is_relative_to(STAGING_DIR.resolve()):
        return "staging"
    for key, preset_dir in SAVE_PRESETS.items():
        if resolved.is_relative_to(preset_dir.resolve()):
            return "saved"
    return "legacy"


def _is_vision_model(name: str) -> bool:
    lower = name.lower()
    return any(hint in lower for hint in VISION_MODEL_HINTS)


def get_preflight_status(ollama_url: str = "http://localhost:11434") -> dict[str, Any]:
    """Inspect Python venv, OS privileges, dependencies, Ollama models, and folder paths."""
    ensure_directories()

    deps = {
        "mss": importlib.util.find_spec("mss") is not None,
        "pynput": importlib.util.find_spec("pynput") is not None,
        "Pillow": importlib.util.find_spec("PIL") is not None,
        "uiautomation": importlib.util.find_spec("uiautomation") is not None,
        "requests": importlib.util.find_spec("requests") is not None,
        "rapidocr_onnxruntime": importlib.util.find_spec("rapidocr_onnxruntime") is not None,
    }

    is_admin = False
    if sys.platform == "win32":
        try:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            is_admin = False

    ollama_info: dict[str, Any] = {
        "online": False,
        "url": ollama_url.rstrip("/"),
        "models": [],
        "recommended_default": "gemma4:e4b",
        "error": "",
    }
    try:
        req = urllib.request.Request(f"{ollama_info['url']}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        models = []
        for item in payload.get("models", []):
            name = str(item.get("name", ""))
            size_bytes = int(item.get("size", 0) or 0)
            models.append(
                {
                    "name": name,
                    "size_gb": round(size_bytes / (1024**3), 2) if size_bytes else 0.0,
                    "is_vision_recommended": _is_vision_model(name),
                }
            )
        models.sort(key=lambda m: (not m["is_vision_recommended"], m["name"]))
        ollama_info["online"] = True
        ollama_info["models"] = models
        names = [m["name"] for m in models]
        if "gemma4:e4b" in names:
            ollama_info["recommended_default"] = "gemma4:e4b"
        else:
            vision = [m["name"] for m in models if m["is_vision_recommended"]]
            if vision:
                ollama_info["recommended_default"] = vision[0]
            elif names:
                ollama_info["recommended_default"] = names[0]
    except Exception as exc:
        ollama_info["error"] = str(exc)

    sessions = list_all_sessions()
    return {
        "python": {
            "version": sys.version.split()[0],
            "executable": sys.executable,
            "in_venv": sys.prefix != sys.base_prefix,
        },
        "platform": {
            "os": sys.platform,
            "dpi_v2_supported": sys.platform == "win32",
            "is_admin": is_admin,
        },
        "dependencies": deps,
        "all_dependencies_ready": all(deps.values()),
        "ollama": ollama_info,
        "folders": {
            "staging": str(STAGING_DIR),
            "workflows": str(WORKFLOWS_DIR),
            "sessions": str(SESSIONS_DIR),
            "directives_sops": str(SAVE_PRESETS["directives_sops"]),
            "knowledge_guides": str(SAVE_PRESETS["knowledge_guides"]),
            "skill_dir": str(SKILL_DIR),
        },
        "counts": {
            "staging": len(sessions["staging"]),
            "saved": len(sessions["saved"]),
            "legacy": len(sessions["legacy"]),
        },
    }


def _read_session_summary(session_dir: Path, category: str) -> Optional[dict[str, Any]]:
    db_path = session_dir / "session.db"
    if not db_path.is_file():
        return None
    try:
        with labguide_engine.connect_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            meta = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
            step_count = int(conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0])
    except Exception:
        return None

    return {
        "id": session_dir.name,
        "path": str(session_dir),
        "title": meta.get("title") or session_dir.name,
        "created_at": meta.get("created_at", ""),
        "saved_at": meta.get("saved_at", ""),
        "status": meta.get("status", category),
        "category": category,
        "step_count": step_count,
        "has_md": (session_dir / "guide.md").is_file(),
        "has_html": (session_dir / "guide.html").is_file(),
    }


def _scan_session_folder(folder: Path, category: str, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    if not folder.is_dir():
        return []
    exclude_set = exclude or set()
    items: list[dict[str, Any]] = []
    subdirs = [d for d in folder.iterdir() if d.is_dir() and d.name not in exclude_set]
    subdirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for sub in subdirs:
        summary = _read_session_summary(sub, category)
        if summary is not None:
            items.append(summary)
    return items


def list_all_sessions() -> dict[str, list[dict[str, Any]]]:
    """List staging drafts, saved workflows, and legacy session folders."""
    ensure_directories()
    staging = _scan_session_folder(STAGING_DIR, "staging")

    saved: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for preset_dir in SAVE_PRESETS.values():
        for item in _scan_session_folder(preset_dir, "saved"):
            resolved = Path(item["path"]).resolve()
            if resolved not in seen_paths:
                seen_paths.add(resolved)
                saved.append(item)
    saved.sort(key=lambda s: s.get("saved_at") or s.get("created_at") or "", reverse=True)

    legacy = _scan_session_folder(SESSIONS_DIR, "legacy", exclude={"_staging"})
    return {"staging": staging, "saved": saved, "legacy": legacy}


def get_session_detail(session_dir: Path) -> dict[str, Any]:
    """Return full metadata, steps, and compiled Markdown for a session."""
    resolved = resolve_safe_session_dir(session_dir)
    category = _classify_category(resolved)
    with labguide_engine.connect_sqlite(resolved / "session.db") as conn:
        conn.row_factory = sqlite3.Row
        meta = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
        steps = [dict(r) for r in conn.execute("SELECT * FROM steps ORDER BY step_num ASC").fetchall()]

    for step in steps:
        crop_rel = str(step.get("crop_image") or "")
        full_rel = str(step.get("full_image") or "")
        raw_name = _derive_raw_filename(full_rel, int(step["step_num"]))
        step["has_raw"] = (resolved / "assets" / "raw" / raw_name).is_file()
        step["crop_exists"] = bool(crop_rel and (resolved / crop_rel).is_file())
        step["full_exists"] = bool(full_rel and (resolved / full_rel).is_file())

    md_path = resolved / "guide.md"
    html_path = resolved / "guide.html"
    return {
        "id": resolved.name,
        "path": str(resolved),
        "category": category,
        "metadata": meta,
        "title": meta.get("title") or resolved.name,
        "steps": steps,
        "has_md": md_path.is_file(),
        "has_html": html_path.is_file(),
        "guide_md": md_path.read_text(encoding="utf-8") if md_path.is_file() else "",
    }


def _derive_raw_filename(full_image_rel: str, step_num: int) -> str:
    if full_image_rel:
        name = Path(full_image_rel).name
        if name.endswith("_full.png"):
            return name.replace("_full.png", "_raw.png")
    return f"step_{step_num:03d}_raw.png"


def _refresh_outputs(session_dir: Path, model: str = "gemma4:e4b", prompt_style: str = "concise") -> None:
    engine = labguide_engine.LabGuideEngine(
        session_dir=session_dir,
        title=None,
        model=model,
        redact_pii=False,
        prompt_style=prompt_style,
        session_status=None,
    )
    engine.compile_guide(use_ollama=False)


def update_session_title(session_dir: Path, new_title: str) -> dict[str, Any]:
    resolved = resolve_safe_session_dir(session_dir)
    clean_title = new_title.strip() or "Standard Operating Procedure"
    with labguide_engine.connect_sqlite(resolved / "session.db") as conn:
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("title", clean_title),
        )
    _refresh_outputs(resolved)
    return get_session_detail(resolved)


def update_step_text(
    session_dir: Path,
    step_num: int,
    step_title: str,
    step_instruction: str,
    step_note: str,
) -> dict[str, Any]:
    resolved = resolve_safe_session_dir(session_dir)
    with labguide_engine.connect_sqlite(resolved / "session.db") as conn:
        cur = conn.execute(
            "UPDATE steps SET step_title = ?, step_instruction = ?, step_note = ? WHERE step_num = ?",
            (step_title.strip(), step_instruction.strip(), step_note.strip(), int(step_num)),
        )
        if cur.rowcount == 0:
            raise ValueError(f"Step {step_num} not found.")
    _refresh_outputs(resolved)
    return get_session_detail(resolved)


def delete_step(session_dir: Path, step_num: int) -> dict[str, Any]:
    resolved = resolve_safe_session_dir(session_dir)
    with labguide_engine.connect_sqlite(resolved / "session.db") as conn:
        cur = conn.execute("DELETE FROM steps WHERE step_num = ?", (int(step_num),))
        if cur.rowcount == 0:
            raise ValueError(f"Step {step_num} not found.")
        remaining = [
            r[0] for r in conn.execute("SELECT step_num FROM steps ORDER BY step_num ASC").fetchall()
        ]
        for idx, old_num in enumerate(remaining, start=1):
            conn.execute("UPDATE steps SET step_num = ? WHERE step_num = ?", (-10000 - idx, old_num))
        for idx in range(1, len(remaining) + 1):
            conn.execute("UPDATE steps SET step_num = ? WHERE step_num = ?", (idx, -10000 - idx))
    _refresh_outputs(resolved)
    return get_session_detail(resolved)


def move_step(session_dir: Path, step_num: int, direction: str) -> dict[str, Any]:
    resolved = resolve_safe_session_dir(session_dir)
    if direction not in {"up", "down"}:
        raise ValueError("Direction must be 'up' or 'down'.")
    target_num = int(step_num) - 1 if direction == "up" else int(step_num) + 1
    with labguide_engine.connect_sqlite(resolved / "session.db") as conn:
        nums = {r[0] for r in conn.execute("SELECT step_num FROM steps").fetchall()}
        if int(step_num) not in nums or target_num not in nums:
            return get_session_detail(resolved)
        conn.execute("UPDATE steps SET step_num = -999999 WHERE step_num = ?", (int(step_num),))
        conn.execute("UPDATE steps SET step_num = ? WHERE step_num = ?", (int(step_num), target_num))
        conn.execute("UPDATE steps SET step_num = ? WHERE step_num = -999999", (target_num,))
    _refresh_outputs(resolved)
    return get_session_detail(resolved)


def apply_manual_blur(
    session_dir: Path,
    step_num: int,
    target: str,
    boxes: list[dict[str, float]],
    mode: str = "blur",
) -> dict[str, Any]:
    """Apply normalized Gaussian blur or solid blackout boxes to a step screenshot."""
    if labguide_engine.Image is None or labguide_engine.ImageFilter is None:
        raise RuntimeError("Pillow is required for manual image redaction.")
    if target not in {"crop", "full"}:
        raise ValueError("Redaction target must be 'crop' or 'full'.")
    if mode not in {"blur", "blackout"}:
        raise ValueError("Redaction mode must be 'blur' or 'blackout'.")

    resolved = resolve_safe_session_dir(session_dir)
    col = "crop_image" if target == "crop" else "full_image"
    with labguide_engine.connect_sqlite(resolved / "session.db") as conn:
        row = conn.execute(
            f"SELECT {col} FROM steps WHERE step_num = ?", (int(step_num),)
        ).fetchone()
    if not row or not row[0]:
        raise ValueError(f"Step {step_num} image not found.")

    img_path = (resolved / row[0]).resolve()
    if not img_path.is_relative_to((resolved / "assets").resolve()) or not img_path.is_file():
        raise FileNotFoundError(f"Image file missing: {img_path}")

    with labguide_engine.Image.open(img_path) as opened:
        img = opened.convert("RGB")
    draw = labguide_engine.ImageDraw.Draw(img) if (mode == "blackout" and labguide_engine.ImageDraw is not None) else None
    for box in boxes:
        x = max(0.0, min(1.0, float(box.get("x", 0.0))))
        y = max(0.0, min(1.0, float(box.get("y", 0.0))))
        w = max(0.0, min(1.0 - x, float(box.get("w", 0.0))))
        h = max(0.0, min(1.0 - y, float(box.get("h", 0.0))))
        left = int(round(x * img.width))
        top = int(round(y * img.height))
        right = int(round((x + w) * img.width))
        bottom = int(round((y + h) * img.height))
        if right - left >= 2 and bottom - top >= 2:
            if mode == "blackout" and draw is not None:
                draw.rectangle((left, top, right, bottom), fill="#090d16")
            else:
                patch = img.crop((left, top, right, bottom)).filter(
                    labguide_engine.ImageFilter.GaussianBlur(radius=14)
                )
                img.paste(patch, (left, top, right, bottom))

    img.save(img_path, "PNG", optimize=True)
    _refresh_outputs(resolved)
    return get_session_detail(resolved)


def reset_step_from_raw(
    session_dir: Path,
    step_num: int,
    redact_pii: bool = False,
) -> dict[str, Any]:
    """Regenerate annotated full & crop PNGs from the untouched raw screenshot."""
    if labguide_engine.Image is None or labguide_engine.ImageDraw is None:
        raise RuntimeError("Pillow is required to reset images from raw captures.")

    resolved = resolve_safe_session_dir(session_dir)
    with labguide_engine.connect_sqlite(resolved / "session.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT x, y, uia_json, full_image, crop_image FROM steps WHERE step_num = ?",
            (int(step_num),),
        ).fetchone()
    if not row:
        raise ValueError(f"Step {step_num} not found.")

    raw_name = _derive_raw_filename(row["full_image"] or "", int(step_num))
    raw_path = resolved / "assets" / "raw" / raw_name
    if not raw_path.is_file():
        raise FileNotFoundError(f"Raw capture missing: {raw_path}")

    uia = json.loads(row["uia_json"] or "{}")
    rect = uia.get("rect")
    mon = uia.get("monitor") or {"left": 0, "top": 0}
    mon_left = int(mon.get("left", 0))
    mon_top = int(mon.get("top", 0))

    with labguide_engine.Image.open(raw_path) as opened:
        full_img = opened.convert("RGB")

    if redact_pii:
        engine = labguide_engine.LabGuideEngine(
            session_dir=resolved,
            title=None,
            redact_pii=True,
            session_status=None,
        )
        clean_img = engine._apply_pii_blur(full_img)
    else:
        clean_img = full_img

    rel_x = max(0, min(clean_img.width - 1, int(row["x"]) - mon_left))
    rel_y = max(0, min(clean_img.height - 1, int(row["y"]) - mon_top))

    if rect and len(rect) == 4:
        c_left = max(0, (int(rect[0]) - mon_left) - 140)
        c_top = max(0, (int(rect[1]) - mon_top) - 90)
        c_right = min(clean_img.width, (int(rect[2]) - mon_left) + 140)
        c_bottom = min(clean_img.height, (int(rect[3]) - mon_top) + 90)
    else:
        c_left = max(0, rel_x - 260)
        c_top = max(0, rel_y - 160)
        c_right = min(clean_img.width, rel_x + 260)
        c_bottom = min(clean_img.height, rel_y + 160)

    if c_right <= c_left or c_bottom <= c_top:
        c_left, c_top, c_right, c_bottom = 0, 0, clean_img.width, clean_img.height

    crop_img = clean_img.crop((c_left, c_top, c_right, c_bottom)).copy()

    annotated = clean_img.copy()
    draw = labguide_engine.ImageDraw.Draw(annotated)
    draw.ellipse((rel_x - 22, rel_y - 22, rel_x + 22, rel_y + 22), outline="#FF3B30", width=4)
    draw.ellipse((rel_x - 6, rel_y - 6, rel_x + 6, rel_y + 6), fill="#FF3B30")

    crop_draw = labguide_engine.ImageDraw.Draw(crop_img)
    cx, cy = rel_x - c_left, rel_y - c_top
    crop_draw.ellipse((cx - 18, cy - 18, cx + 18, cy + 18), outline="#FF3B30", width=3)

    annotated.save(resolved / row["full_image"], "PNG", optimize=True)
    crop_img.save(resolved / row["crop_image"], "PNG", optimize=True)
    _refresh_outputs(resolved)
    return get_session_detail(resolved)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:60] or "workflow"


def save_session_to_workflows(
    session_dir: Path,
    preset_key: str = "workflows",
    custom_name: str = "",
    keep_staging: bool = False,
) -> dict[str, Any]:
    """Promote/save a reviewed session into the permanent workflows library."""
    resolved = resolve_safe_session_dir(session_dir)
    if preset_key not in SAVE_PRESETS:
        raise ValueError(f"Unknown destination preset: {preset_key}")

    dest_root = SAVE_PRESETS[preset_key]
    dest_root.mkdir(parents=True, exist_ok=True)

    detail = get_session_detail(resolved)
    title_slug = _slugify(custom_name or detail["title"])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder_name = f"{stamp}_{title_slug}"
    dest_dir = (dest_root / folder_name).resolve()

    if not dest_dir.is_relative_to(dest_root.resolve()):
        raise ValueError("Invalid destination folder name.")

    if resolved == dest_dir:
        return {"saved_path": str(dest_dir), "detail": detail}

    shutil.copytree(resolved, dest_dir)
    with labguide_engine.connect_sqlite(dest_dir / "session.db") as conn:
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("status", "saved"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("saved_at", datetime.now().isoformat()),
        )
    _refresh_outputs(dest_dir)

    if _classify_category(resolved) == "staging" and not keep_staging:
        shutil.rmtree(resolved)

    return {"saved_path": str(dest_dir), "detail": get_session_detail(dest_dir)}


def discard_session(session_dir: Path) -> None:
    """Permanently delete a staging draft or workflow session."""
    resolved = resolve_safe_session_dir(session_dir, require_db=False)
    shutil.rmtree(resolved)


def update_iso_metadata(
    session_dir: Path,
    doc_id: str = "",
    version: str = "1.0",
    owner: str = "",
    effective_date: str = "",
    prerequisites: str = "",
    safety_warnings: str = "",
) -> dict[str, Any]:
    """Update ISO compliance header metadata and regenerate guide outputs."""
    resolved = resolve_safe_session_dir(session_dir)
    pairs = {
        "doc_id": doc_id.strip() or f"SOP-{datetime.now().strftime('%Y%m%d')}",
        "version": version.strip() or "1.0",
        "owner": owner.strip() or brand.PUBLISHER,
        "effective_date": effective_date.strip() or datetime.now().strftime("%Y-%m-%d"),
        "prerequisites": prerequisites.strip(),
        "safety_warnings": safety_warnings.strip(),
    }
    with labguide_engine.connect_sqlite(resolved / "session.db") as conn:
        for k, v in pairs.items():
            conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (k, v))
    _refresh_outputs(resolved)
    return get_session_detail(resolved)


def replace_step_image(session_dir: Path, step_num: int, image_bytes: bytes) -> dict[str, Any]:
    """Replace a single step's screenshot without re-recording the entire workflow."""
    if labguide_engine.Image is None:
        raise RuntimeError("Pillow is required to replace step images.")
    resolved = resolve_safe_session_dir(session_dir)
    with labguide_engine.connect_sqlite(resolved / "session.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT full_image, crop_image FROM steps WHERE step_num = ?", (int(step_num),)
        ).fetchone()
    if not row:
        raise ValueError(f"Step {step_num} not found.")

    raw_name = _derive_raw_filename(row["full_image"] or "", int(step_num))
    raw_path = resolved / "assets" / "raw" / raw_name
    full_path = resolved / row["full_image"]
    crop_path = resolved / row["crop_image"]

    import io as _io

    with labguide_engine.Image.open(_io.BytesIO(image_bytes)) as opened:
        rgb = opened.convert("RGB")
        rgb.save(raw_path, "PNG")
        rgb.save(full_path, "PNG", optimize=True)
        crop = rgb.copy()
        crop.thumbnail((1280, 720))
        crop.save(crop_path, "PNG", optimize=True)

    _refresh_outputs(resolved)
    return get_session_detail(resolved)


def generate_session_quiz(
    session_dir: Path,
    use_ollama: bool = True,
    model: str = "gemma4:e4b",
    ollama_url: str = "http://localhost:11434",
) -> dict[str, Any]:
    """Generate 3 multiple-choice verification questions via Ollama (with offline fallback)."""
    resolved = resolve_safe_session_dir(session_dir)
    with labguide_engine.connect_sqlite(resolved / "session.db") as conn:
        conn.row_factory = sqlite3.Row
        meta = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
        steps = [dict(r) for r in conn.execute("SELECT * FROM steps ORDER BY step_num ASC").fetchall()]
    if not steps:
        raise ValueError("Cannot generate a knowledge check for a session with 0 steps.")

    title = meta.get("title") or resolved.name
    fallback_quiz = [
        {
            "question": f"What is the primary action performed in Step 1 of '{title}'?",
            "options": [
                steps[0].get("step_title") or "Initialize the procedure",
                "Delete the workflow archive immediately",
                "Skip pre-flight verification checks",
                "Upload local credentials to an external server",
            ],
            "correct_index": 0,
            "explanation": f"Step 1 begins with: {steps[0].get('step_instruction') or steps[0].get('step_title')}.",
        },
        {
            "question": "How should sensitive credentials or custom project codes be handled in LabGuide Studio?",
            "options": [
                "Leave them visible in plain text",
                "Use password-aware masking, CPU RapidOCR, or Manual Blur/Blackout before saving",
                "Send unredacted screenshots to external cloud APIs",
                "Delete assets/raw/ permanently",
            ],
            "correct_index": 1,
            "explanation": "LabGuide Studio masks passwords as •••••••• and provides local Blur/Blackout redaction.",
        },
        {
            "question": f"How many verified steps are included in '{title}'?",
            "options": [
                "Only 1 step regardless of workflow",
                f"Exactly {len(steps)} step(s)",
                "Always 25 steps",
                "0 steps",
            ],
            "correct_index": 1,
            "explanation": f"This SOP contains {len(steps)} documented step(s).",
        },
    ]

    quiz = fallback_quiz
    if use_ollama:
        summary_lines = "\n".join(
            f"- Step {s['step_num']}: {s.get('step_title', '')} — {s.get('step_instruction', '')}"
            for s in steps[:12]
        )
        prompt = (
            f"Create 3 multiple-choice comprehension questions for trainees reading the SOP '{title}'.\n"
            f"Steps:\n{summary_lines}\n\n"
            'Return ONLY valid JSON: {"questions": [{"question": "...", "options": ["A","B","C","D"], '
            '"correct_index": 0, "explanation": "..."}]}'
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
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            parsed = json.loads(body.get("response", "{}"))
            qs = parsed.get("questions")
            if isinstance(qs, list) and len(qs) >= 1:
                cleaned = []
                for q in qs[:3]:
                    opts = [str(o) for o in (q.get("options") or [])][:4]
                    if len(opts) >= 2:
                        cidx = max(0, min(len(opts) - 1, int(q.get("correct_index", 0))))
                        cleaned.append(
                            {
                                "question": str(q.get("question") or "").strip(),
                                "options": opts,
                                "correct_index": cidx,
                                "explanation": str(q.get("explanation") or "").strip(),
                            }
                        )
                if cleaned:
                    quiz = cleaned
        except Exception:
            quiz = fallback_quiz

    with labguide_engine.connect_sqlite(resolved / "session.db") as conn:
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("quiz_json", json.dumps(quiz)),
        )
    _refresh_outputs(resolved)
    return get_session_detail(resolved)
