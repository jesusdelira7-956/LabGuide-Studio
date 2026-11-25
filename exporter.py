from __future__ import annotations

import base64
import html
import io
import json
import re
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import brand  # noqa: E402

_SKILL_SCRIPTS = brand.get_skill_dir() / "scripts"
SCRIPTS_DIR = _SKILL_SCRIPTS if _SKILL_SCRIPTS.is_dir() else PROJECT_DIR
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import labguide_engine  # noqa: E402
import session_manager  # noqa: E402


def _slug_filename(title: str, fallback: str = "workflow") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (title or "").strip().lower()).strip("-")
    return slug[:60] or fallback


def _load_session(session_dir: Path) -> tuple[dict[str, str], list[dict[str, Any]], str]:
    resolved = session_manager.resolve_safe_session_dir(session_dir)
    with labguide_engine.connect_sqlite(resolved / "session.db") as conn:
        conn.row_factory = sqlite3.Row
        meta = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
        steps = [dict(r) for r in conn.execute("SELECT * FROM steps ORDER BY step_num ASC").fetchall()]
    title = meta.get("title") or resolved.name
    return meta, steps, title


def export_markdown(session_dir: Path, embed_base64: bool = False) -> tuple[bytes, str, str]:
    resolved = session_manager.resolve_safe_session_dir(session_dir)
    md_path = resolved / "guide.md"
    if not md_path.is_file():
        engine = labguide_engine.LabGuideEngine(
            session_dir=resolved,
            title=None,
            redact_pii=False,
            session_status=None,
        )
        engine.compile_guide(use_ollama=False)

    meta, _steps, title = _load_session(resolved)
    text = md_path.read_text(encoding="utf-8") if md_path.is_file() else f"# {title}\n"

    if embed_base64:
        def _replace(match: re.Match[str]) -> str:
            rel = match.group(1)
            img_path = (resolved / rel).resolve()
            if img_path.is_relative_to((resolved / "assets").resolve()) and img_path.is_file():
                b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
                return f"data:image/png;base64,{b64}"
            return match.group(0)

        text = re.sub(r"\./(assets/[^\)\s]+)", _replace, text)

    slug = _slug_filename(meta.get("title", title))
    suffix = "_self_contained.md" if embed_base64 else ".md"
    return text.encode("utf-8"), "text/markdown; charset=utf-8", f"{slug}{suffix}"


def export_html(session_dir: Path) -> tuple[bytes, str, str]:
    resolved = session_manager.resolve_safe_session_dir(session_dir)
    html_path = resolved / "guide.html"
    if not html_path.is_file():
        engine = labguide_engine.LabGuideEngine(
            session_dir=resolved,
            title=None,
            redact_pii=False,
            session_status=None,
        )
        engine.compile_guide(use_ollama=False)
    _meta, _steps, title = _load_session(resolved)
    slug = _slug_filename(title)
    content = html_path.read_bytes() if html_path.is_file() else b""
    return content, "text/html; charset=utf-8", f"{slug}.html"


def export_pdf_print_html(session_dir: Path) -> bytes:
    resolved = session_manager.resolve_safe_session_dir(session_dir)
    meta, steps, title = _load_session(resolved)
    created = meta.get("created_at", datetime.now().isoformat())

    blocks: list[str] = []
    for step in steps:
        s_title = step.get("step_title") or f"Step {step['step_num']}"
        s_instr = step.get("step_instruction") or ""
        s_note = step.get("step_note") or ""
        crop_rel = step.get("crop_image") or ""
        crop_path = resolved / crop_rel if crop_rel else None
        img_tag = ""
        if crop_path and crop_path.is_file():
            b64 = base64.b64encode(crop_path.read_bytes()).decode("ascii")
            img_tag = f'<img src="data:image/png;base64,{b64}" alt="Step {step["step_num"]}" />'
        note_tag = f'<div class="note"><strong>Note:</strong> {html.escape(s_note)}</div>' if s_note else ""
        blocks.append(
            f"""<section class="step">
  <h2>Step {step['step_num']}: {html.escape(s_title)}</h2>
  <p>{html.escape(s_instr)}</p>
  {note_tag}
  {img_tag}
</section>"""
        )

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{html.escape(title)} (Print / PDF)</title>
  <style>
    @page {{ margin: 16mm; }}
    body {{
      font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
      color: #0f172a;
      background: #ffffff;
      max-width: 820px;
      margin: 0 auto;
      padding: 1rem;
      line-height: 1.5;
    }}
    h1 {{ margin-bottom: 0.2rem; border-bottom: 2px solid #0284c7; padding-bottom: 0.4rem; }}
    .meta {{ color: #475569; font-size: 0.9rem; margin-bottom: 1.5rem; }}
    .step {{
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      padding: 1rem;
      margin-bottom: 1rem;
      break-inside: avoid;
      page-break-inside: avoid;
    }}
    .step h2 {{ margin-top: 0; font-size: 1.1rem; color: #0f172a; }}
    .note {{
      background: #f1f5f9;
      border-left: 3px solid #0284c7;
      padding: 0.5rem 0.75rem;
      margin: 0.5rem 0 0.75rem;
      font-size: 0.92rem;
    }}
    img {{
      max-width: 100%;
      max-height: 360px;
      object-fit: contain;
      border: 1px solid #cbd5e1;
      border-radius: 4px;
      display: block;
    }}
    @media print {{ .no-print {{ display: none; }} }}
  </style>
</head>
<body>
  <div class="no-print" style="margin-bottom:1rem;">
    <button type="button" onclick="window.print()" style="padding:0.5rem 1rem;cursor:pointer;">Print / Save as PDF</button>
  </div>
  <h1>{html.escape(title)}</h1>
  <p class="meta">Created: {html.escape(created)} &bull; Steps: {len(steps)}</p>
  {"".join(blocks)}
  <script>window.addEventListener("load", () => setTimeout(() => window.print(), 250));</script>
</body>
</html>"""
    return doc.encode("utf-8")


def _image_emu_size(img_path: Path, max_width_inches: float = 5.8) -> tuple[int, int]:
    w_px, h_px = 800, 480
    if labguide_engine.Image is not None:
        try:
            with labguide_engine.Image.open(img_path) as im:
                w_px, h_px = im.size
        except Exception:
            pass
    w_px = max(1, w_px)
    h_px = max(1, h_px)
    emu_per_inch = 914400
    target_w = min(int(max_width_inches * emu_per_inch), w_px * 9525)
    target_h = int(target_w * (h_px / w_px))
    return target_w, target_h


def _docx_paragraph(text: str, style: str | None = None, bold: bool = False) -> str:
    p_pr = f'<w:pPr><w:pStyle w:val="{xml_escape(style)}"/></w:pPr>' if style else ""
    r_pr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return (
        f"<w:p>{p_pr}<w:r>{r_pr}"
        f'<w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r></w:p>'
    )


def _docx_image_paragraph(rel_id: str, doc_pr_id: int, name: str, cx: int, cy: int) -> str:
    safe_name = xml_escape(name)
    return (
        "<w:p><w:r><w:drawing>"
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/>'
        f'<wp:docPr id="{doc_pr_id}" name="{safe_name}"/>'
        "<wp:cNvGraphicFramePr>"
        '<a:graphicFrameLocks xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" noChangeAspect="1"/>'
        "</wp:cNvGraphicFramePr>"
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        "<pic:nvPicPr>"
        f'<pic:cNvPr id="{doc_pr_id}" name="{safe_name}"/>'
        "<pic:cNvPicPr/>"
        "</pic:nvPicPr>"
        "<pic:blipFill>"
        f'<a:blip r:embed="{rel_id}"/>'
        "<a:stretch><a:fillRect/></a:stretch>"
        "</pic:blipFill>"
        "<pic:spPr>"
        f'<a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        "</pic:spPr>"
        "</pic:pic>"
        "</a:graphicData>"
        "</a:graphic>"
        "</wp:inline></w:drawing></w:r></w:p>"
    )


def export_docx(session_dir: Path) -> tuple[bytes, str, str]:
    """Build a self-contained OOXML .docx SOP document with embedded PNG screenshots."""
    resolved = session_manager.resolve_safe_session_dir(session_dir)
    meta, steps, title = _load_session(resolved)
    slug = _slug_filename(title)

    media_files: list[tuple[str, bytes]] = []
    rel_entries: list[str] = [
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    ]

    body_parts: list[str] = [
        _docx_paragraph(title, style="Title"),
        _docx_paragraph(f"Generated locally via {brand.APP_NAME} ({brand.PUBLISHER}, {meta.get('created_at', '')})"),
    ]
    if any(meta.get(k) for k in ("doc_id", "version", "owner", "effective_date")):
        body_parts.append(
            _docx_paragraph(
                f"Document ID: {meta.get('doc_id', 'SOP-DRAFT')} | "
                f"Version: {meta.get('version', '1.0')} | "
                f"Owner: {meta.get('owner', brand.PUBLISHER)} | "
                f"Effective: {meta.get('effective_date', '')}",
                bold=True,
            )
        )
        if meta.get("prerequisites"):
            body_parts.append(_docx_paragraph(f"Prerequisites: {meta['prerequisites']}"))
        if meta.get("safety_warnings"):
            body_parts.append(_docx_paragraph(f"Warning: {meta['safety_warnings']}", bold=True))

    next_rid = 2
    doc_pr_id = 1
    for step in steps:
        s_title = step.get("step_title") or f"Step {step['step_num']}"
        s_instr = step.get("step_instruction") or ""
        s_note = step.get("step_note") or ""

        body_parts.append(_docx_paragraph(f"Step {step['step_num']}: {s_title}", style="Heading1"))
        if s_instr:
            body_parts.append(_docx_paragraph(s_instr))
        if s_note:
            body_parts.append(_docx_paragraph(f"Note: {s_note}", bold=True))

        crop_rel = step.get("crop_image") or ""
        crop_path = resolved / crop_rel if crop_rel else None
        if crop_path and crop_path.is_file():
            media_name = f"step_{step['step_num']:03d}.png"
            media_files.append((media_name, crop_path.read_bytes()))
            rid = f"rId{next_rid}"
            next_rid += 1
            rel_entries.append(
                f'<Relationship Id="{rid}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                f'Target="media/{media_name}"/>'
            )
            cx, cy = _image_emu_size(crop_path)
            body_parts.append(_docx_image_paragraph(rid, doc_pr_id, media_name, cx, cy))
            doc_pr_id += 1

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
        f"<w:body>{''.join(body_parts)}<w:sectPr/></w:body></w:document>"
    )

    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/><w:rPr><w:sz w:val="22"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>'
        '<w:rPr><w:b/><w:sz w:val="40"/><w:color w:val="0284C7"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
        '<w:pPr><w:spacing w:before="240" w:after="80"/></w:pPr>'
        '<w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>'
        "</w:styles>"
    )

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )

    root_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )

    doc_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{''.join(rel_entries)}</Relationships>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", root_rels_xml)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/styles.xml", styles_xml)
        zf.writestr("word/_rels/document.xml.rels", doc_rels_xml)
        for media_name, data in media_files:
            zf.writestr(f"word/media/{media_name}", data)

    return (
        buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        f"{slug}.docx",
    )


def export_json_bundle(session_dir: Path) -> tuple[bytes, str, str]:
    resolved = session_manager.resolve_safe_session_dir(session_dir)
    meta, steps, title = _load_session(resolved)
    slug = _slug_filename(title)

    clean_steps: list[dict[str, Any]] = []
    for step in steps:
        uia = json.loads(step.get("uia_json") or "{}")
        clean_steps.append(
            {
                "step_num": step["step_num"],
                "timestamp": step["timestamp"],
                "event_type": step["event_type"],
                "x": step["x"],
                "y": step["y"],
                "window_title": step["window_title"],
                "control_name": step["control_name"],
                "control_type": step["control_type"],
                "step_title": step.get("step_title") or "",
                "step_instruction": step.get("step_instruction") or "",
                "step_note": step.get("step_note") or "",
                "uia": uia,
            }
        )

    payload = {
        "schema": "labguide-studio/v1",
        "publisher": brand.PUBLISHER,
        "title": title,
        "metadata": meta,
        "steps": clean_steps,
    }
    return (
        json.dumps(payload, indent=2).encode("utf-8"),
        "application/json; charset=utf-8",
        f"{slug}.json",
    )


def export_replay_script(session_dir: Path) -> tuple[bytes, str, str]:
    """Generate an executable UIA + pynput Antigravity replay script for recorded steps."""
    resolved = session_manager.resolve_safe_session_dir(session_dir)
    _meta, steps, title = _load_session(resolved)
    slug = _slug_filename(title)

    replay_steps: list[dict[str, Any]] = []
    for step in steps:
        uia = json.loads(step.get("uia_json") or "{}")
        replay_steps.append(
            {
                "step_num": step["step_num"],
                "event_type": step["event_type"],
                "x": step["x"],
                "y": step["y"],
                "window_title": step.get("window_title") or "",
                "control_name": step.get("control_name") or "",
                "control_type": step.get("control_type") or "",
                "automation_id": uia.get("automation_id") or "",
                "typed_text": uia.get("typed_text") or "",
                "is_sensitive": bool(uia.get("is_sensitive", False)),
            }
        )

    steps_literal = json.dumps(replay_steps, indent=4)
    script = f'''#!/usr/bin/env python3
"""Auto-generated LabGuide Studio (Jesse in the Lab) Windows 11 Replay Script: {title}"""
from __future__ import annotations

import ctypes
import getpass
import sys
import time

try:
    import uiautomation as auto
except ImportError:
    auto = None

from pynput import keyboard, mouse

STEPS = {steps_literal}


def enable_dpi() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def find_control(step: dict) -> tuple[int, int]:
    x, y = int(step["x"]), int(step["y"])
    if auto is None:
        return x, y
    try:
        win_title = step.get("window_title") or ""
        auto_id = step.get("automation_id") or ""
        ctrl_name = step.get("control_name") or ""
        root = auto.WindowControl(searchDepth=1, SubName=win_title) if win_title else auto.GetRootControl()
        if root.Exists(maxSearchSeconds=1.5):
            ctrl = None
            if auto_id:
                ctrl = root.Control(AutomationId=auto_id)
            elif ctrl_name:
                ctrl = root.Control(Name=ctrl_name)
            if ctrl and ctrl.Exists(maxSearchSeconds=1.0):
                rect = ctrl.BoundingRectangle
                if rect and rect.width() > 0 and rect.height() > 0:
                    return (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
    except Exception:
        pass
    return x, y


def main() -> None:
    enable_dpi()
    m = mouse.Controller()
    k = keyboard.Controller()

    for step in STEPS:
        ev = step["event_type"]
        print(f"[Replay Step {{step['step_num']}}] {{ev}} -> {{step.get('control_name') or step.get('window_title')}}")
        if ev == "click":
            tx, ty = find_control(step)
            m.position = (tx, ty)
            time.sleep(0.1)
            m.click(mouse.Button.left, 1)
        elif ev == "type":
            tx, ty = find_control(step)
            if tx > 0 and ty > 0:
                m.position = (tx, ty)
                m.click(mouse.Button.left, 1)
                time.sleep(0.1)
            text = step.get("typed_text") or ""
            if step.get("is_sensitive") or text == "••••••••":
                text = getpass.getpass(f"Enter masked input for {{step.get('control_name') or 'field'}}: ")
            k.type(text)
        elif ev == "shortcut":
            combo = (step.get("control_name") or "").lower()
            if combo.startswith("ctrl+") and len(combo) == 6:
                with k.pressed(keyboard.Key.ctrl_l):
                    k.press(combo[-1])
                    k.release(combo[-1])
        time.sleep(0.6)


if __name__ == "__main__":
    main()
'''
    return script.encode("utf-8"), "text/x-python; charset=utf-8", f"{slug}_replay.py"


def export_srt(session_dir: Path) -> tuple[bytes, str, str]:
    resolved = session_manager.resolve_safe_session_dir(session_dir)
    _meta, steps, title = _load_session(resolved)
    slug = _slug_filename(title)
    srt_path = resolved / "walkthrough.srt"
    if srt_path.is_file():
        return srt_path.read_bytes(), "text/plain; charset=utf-8", f"{slug}.srt"

    import tts_engine

    timings = []
    cursor = 0.0
    for s in steps:
        txt = f"Step {s['step_num']}: {s.get('step_title', '')}. {s.get('step_instruction', '')}"
        dur = max(3.0, len(txt) / 14.0)
        timings.append({"step_num": s["step_num"], "text": txt, "start_sec": cursor, "duration_sec": dur})
        cursor += dur
    return tts_engine.build_srt_subtitles(timings).encode("utf-8"), "text/plain; charset=utf-8", f"{slug}.srt"


def export_offline_bundle_zip(session_dir: Path) -> tuple[bytes, str, str]:
    """Bundle guide.html, guide.md, guide.docx, MP4, SRT, and PNGs into a portable ZIP."""
    resolved = session_manager.resolve_safe_session_dir(session_dir)
    _meta, _steps, title = _load_session(resolved)
    slug = _slug_filename(title)

    html_bytes, _, _ = export_html(resolved)
    md_bytes, _, _ = export_markdown(resolved, embed_base64=False)
    docx_bytes, _, _ = export_docx(resolved)
    srt_bytes, _, _ = export_srt(resolved)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("guide.html", html_bytes)
        zf.writestr("guide.md", md_bytes)
        zf.writestr("guide.docx", docx_bytes)
        zf.writestr("walkthrough.srt", srt_bytes)
        mp4 = resolved / "walkthrough.mp4"
        if mp4.is_file():
            zf.write(mp4, "walkthrough.mp4")
        assets_dir = resolved / "assets"
        if assets_dir.is_dir():
            for fp in assets_dir.rglob("*.png"):
                if "raw" not in fp.parts:
                    zf.write(fp, fp.relative_to(resolved).as_posix())
    return buf.getvalue(), "application/zip", f"{slug}_offline_bundle.zip"


def export_scorm12_zip(session_dir: Path) -> tuple[bytes, str, str]:
    """Package guide.html, quiz tracking, and media into a SCORM 1.2 LMS ZIP."""
    resolved = session_manager.resolve_safe_session_dir(session_dir)
    meta, _steps, title = _load_session(resolved)
    slug = _slug_filename(title)
    doc_id = xml_escape(meta.get("doc_id") or slug)
    safe_title = xml_escape(title)

    html_bytes, _, _ = export_html(resolved)
    html_text = html_bytes.decode("utf-8")
    if "scorm_wrapper.js" not in html_text:
        html_text = html_text.replace("</body>", '  <script src="scorm_wrapper.js"></script>\n</body>')

    scorm_js = """(function () {
  function findApi(win) {
    var depth = 0;
    while (win && !win.API && win.parent && win.parent !== win && depth < 10) {
      depth++;
      win = win.parent;
    }
    return win && win.API ? win.API : null;
  }
  var api = findApi(window);
  if (api && typeof api.LMSInitialize === "function") {
    api.LMSInitialize("");
    api.LMSSetValue("cmi.core.lesson_status", "incomplete");
    api.LMSCommit("");
  }
  window.labGuideReportScormScore = function (pct) {
    if (!api) return;
    api.LMSSetValue("cmi.core.score.raw", String(pct));
    api.LMSSetValue("cmi.core.lesson_status", pct >= 70 ? "passed" : "completed");
    api.LMSCommit("");
  };
  window.addEventListener("beforeunload", function () {
    if (api && typeof api.LMSFinish === "function") {
      api.LMSFinish("");
    }
  });
})();
"""

    imsmanifest = f"""<?xml version="1.0" encoding="UTF-8"?>
<manifest identifier="com.jesseinthelab.labguide.{doc_id}" version="1.0"
          xmlns="http://www.imsproject.org/xsd/imscp_rootv1p1p2"
          xmlns:adlcp="http://www.adlnet.org/xsd/adlcp_rootv1p2"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <metadata>
    <schema>ADL SCORM</schema>
    <schemaversion>1.2</schemaversion>
  </metadata>
  <organizations default="org_{doc_id}">
    <organization identifier="org_{doc_id}">
      <title>{safe_title}</title>
      <item identifier="item_{doc_id}" identifierref="res_{doc_id}">
        <title>{safe_title}</title>
      </item>
    </organization>
  </organizations>
  <resources>
    <resource identifier="res_{doc_id}" type="webcontent" adlcp:scormtype="sco" href="index.html">
      <file href="index.html"/>
      <file href="scorm_wrapper.js"/>
    </resource>
  </resources>
</manifest>
"""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("imsmanifest.xml", imsmanifest.encode("utf-8"))
        zf.writestr("scorm_wrapper.js", scorm_js.encode("utf-8"))
        zf.writestr("index.html", html_text.encode("utf-8"))
        mp4 = resolved / "walkthrough.mp4"
        if mp4.is_file():
            zf.write(mp4, "walkthrough.mp4")
        srt = resolved / "walkthrough.srt"
        if srt.is_file():
            zf.write(srt, "walkthrough.srt")
    return buf.getvalue(), "application/zip", f"{slug}_scorm12.zip"
