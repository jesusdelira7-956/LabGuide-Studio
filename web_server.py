from __future__ import annotations

import argparse
import base64
import http.server
import json
import mimetypes
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlparse

PROJECT_DIR = Path(__file__).resolve().parent
UI_DIR = PROJECT_DIR / "ui"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import brand  # noqa: E402

_SKILL_SCRIPTS = brand.get_skill_dir() / "scripts"
SCRIPTS_DIR = _SKILL_SCRIPTS if _SKILL_SCRIPTS.is_dir() else PROJECT_DIR
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import exporter  # noqa: E402
import labguide_engine  # noqa: E402
import mini_hud  # noqa: E402
import presenter_engine  # noqa: E402
import session_manager  # noqa: E402
import sop_importer  # noqa: E402
import tts_engine  # noqa: E402
import video_studio  # noqa: E402


class ActiveRecorderController:
    """Thread-safe controller bridging HTTP Studio requests, Mini-HUD, and LabGuideEngine."""

    def __init__(self, enable_hud: bool = True) -> None:
        self._lock = threading.Lock()
        self.mode: str = "idle"  # idle | countdown | recording | paused | compiling
        self.countdown_remaining: int = 0
        self.session_dir: Optional[Path] = None
        self.engine: Optional[labguide_engine.LabGuideEngine] = None
        self.last_completed_session: Optional[str] = None
        self.last_error: str = ""
        self._cancel_countdown = threading.Event()
        self._enable_hud = enable_hud
        self._hud = (
            mini_hud.MiniHudManager(on_pause=self._hud_pause, on_stop=self._hud_stop)
            if enable_hud
            else None
        )

    def _hud_pause(self) -> None:
        try:
            self.toggle_pause()
        except Exception:
            pass

    def _hud_stop(self) -> None:
        try:
            self.stop()
        except Exception:
            pass

    def _on_step_captured(self, step_num: int, _label: str) -> None:
        if self._hud is not None:
            with self._lock:
                mode = self.mode
            self._hud.update(mode=mode, step_count=step_num)

    def start(
        self,
        title: str = "Standard Operating Procedure",
        model: str = "gemma4:e4b",
        ollama_url: str = "http://localhost:11434",
        redact_pii: bool = True,
        auto_compile: bool = False,
        prompt_style: str = "concise",
        capture_shortcuts: bool = True,
        countdown_seconds: int = 3,
        exclude_self: bool = True,
        coalesce_typing: bool = True,
        keep_alive: str = "0",
        show_hud: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            if self.mode != "idle":
                raise RuntimeError(f"Recorder is currently {self.mode}. Stop it before starting a new session.")

            session_manager.ensure_directories()
            session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            session_dir = session_manager.STAGING_DIR / session_id

            engine = labguide_engine.LabGuideEngine(
                session_dir=session_dir,
                title=title.strip() or "Standard Operating Procedure",
                ollama_url=ollama_url,
                model=model,
                redact_pii=redact_pii,
                auto_compile=auto_compile,
                prompt_style=prompt_style,
                capture_shortcuts=capture_shortcuts,
                session_status="staging",
                exclude_self=exclude_self,
                coalesce_typing=coalesce_typing,
                keep_alive=keep_alive,
                on_step_captured=self._on_step_captured,
            )
            self.session_dir = session_dir
            self.engine = engine
            self.last_error = ""
            self._cancel_countdown.clear()

            if countdown_seconds > 0:
                self.mode = "countdown"
                self.countdown_remaining = int(countdown_seconds)
                if self._hud is not None and show_hud:
                    self._hud.show(mode="countdown", step_count=0, countdown=self.countdown_remaining)
                threading.Thread(
                    target=self._countdown_and_start,
                    args=(int(countdown_seconds), show_hud),
                    daemon=True,
                ).start()
            else:
                self.countdown_remaining = 0
                engine.start_async(on_auto_stop=self._handle_hotkey_stop)
                self.mode = "recording"
                if self._hud is not None and show_hud:
                    self._hud.show(mode="recording", step_count=0)

        return self.get_status()

    def _countdown_and_start(self, seconds: int, show_hud: bool) -> None:
        deadline = time.monotonic() + seconds
        while not self._cancel_countdown.is_set():
            rem = max(0, int(round(deadline - time.monotonic())))
            with self._lock:
                self.countdown_remaining = rem
            if self._hud is not None and show_hud:
                self._hud.update(mode="countdown", step_count=0, countdown=rem)
            if rem <= 0:
                break
            time.sleep(0.2)

        if self._cancel_countdown.is_set():
            return

        with self._lock:
            engine = self.engine
            if engine is None or self.mode != "countdown":
                return
            try:
                engine.start_async(on_auto_stop=self._handle_hotkey_stop)
                self.mode = "recording"
                self.countdown_remaining = 0
                if self._hud is not None and show_hud:
                    self._hud.update(mode="recording", step_count=0)
            except Exception as exc:
                self.last_error = str(exc)
                self.mode = "idle"
                self.engine = None
                if self._hud is not None:
                    self._hud.hide()

    def _handle_hotkey_stop(self) -> None:
        with self._lock:
            if self.session_dir is not None:
                self.last_completed_session = str(self.session_dir)
            self.mode = "idle"
            self.engine = None
        if self._hud is not None:
            self._hud.hide()

    def toggle_pause(self) -> dict[str, Any]:
        with self._lock:
            if self.engine is None or self.mode not in {"recording", "paused"}:
                raise RuntimeError("No active recording to pause or resume.")
            is_paused = self.engine.toggle_pause()
            self.mode = "paused" if is_paused else "recording"
            step_count = self.engine.step_counter
            mode = self.mode
        if self._hud is not None:
            self._hud.update(mode=mode, step_count=step_count)
        return self.get_status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self.mode == "idle":
                raise RuntimeError("No recording is currently active.")
            if self.mode == "countdown":
                self._cancel_countdown.set()
                session_dir = self.session_dir
                self.mode = "idle"
                self.countdown_remaining = 0
                self.engine = None
                if session_dir and session_dir.exists():
                    session_manager.discard_session(session_dir)
                self.session_dir = None
                if self._hud is not None:
                    self._hud.hide()
                return {"cancelled_countdown": True, "status": self.get_status(), "session": None}

            engine = self.engine
            session_dir = self.session_dir
            step_count = engine.step_counter if engine else 0
            self.mode = "compiling"

        if self._hud is not None:
            self._hud.update(mode="compiling", step_count=step_count)

        try:
            if engine is not None:
                engine.stop_and_finalize()
        finally:
            with self._lock:
                if session_dir is not None:
                    self.last_completed_session = str(session_dir)
                self.mode = "idle"
                self.engine = None
            if self._hud is not None:
                self._hud.hide()

        detail = (
            session_manager.get_session_detail(session_dir)
            if session_dir and (session_dir / "session.db").is_file()
            else None
        )
        return {"cancelled_countdown": False, "status": self.get_status(), "session": detail}

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            mode = self.mode
            countdown_remaining = self.countdown_remaining
            session_dir = self.session_dir
            engine = self.engine
            last_completed = self.last_completed_session
            last_error = self.last_error

        steps: list[dict[str, Any]] = []
        if session_dir is not None and (session_dir / "session.db").is_file():
            try:
                with labguide_engine.connect_sqlite(session_dir / "session.db") as conn:
                    conn.row_factory = sqlite3.Row
                    steps = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT step_num, event_type, window_title, control_name, control_type "
                            "FROM steps ORDER BY step_num DESC LIMIT 8"
                        ).fetchall()
                    ]
            except Exception:
                steps = []

        return {
            "mode": mode,
            "countdown_remaining": countdown_remaining,
            "session_dir": str(session_dir) if session_dir else "",
            "title": engine.title if engine else "",
            "model": engine.model if engine else "",
            "recent_steps": list(reversed(steps)),
            "step_count": steps[0]["step_num"] if steps else 0,
            "last_completed_session": last_completed or "",
            "last_error": last_error,
        }


class LabGuideHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[http.server.BaseHTTPRequestHandler],
        controller: Optional[ActiveRecorderController] = None,
    ) -> None:
        self.controller = controller or ActiveRecorderController()
        super().__init__(server_address, RequestHandlerClass)


class LabGuideHTTPHandler(http.server.BaseHTTPRequestHandler):
    server: LabGuideHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _is_local_origin(self) -> bool:
        origin = self.headers.get("Origin", "").strip()
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.hostname in {"127.0.0.1", "localhost"}

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file_range(self, file_path: Path, content_type: str) -> None:
        size = file_path.stat().st_size
        range_header = self.headers.get("Range", "").strip()
        if range_header.startswith("bytes="):
            spec = range_header[6:].split(",")[0].strip()
            parts = spec.split("-")
            try:
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if len(parts) > 1 and parts[1] else size - 1
            except ValueError:
                start, end = 0, size - 1
            start = max(0, min(start, max(0, size - 1)))
            end = max(start, min(end, max(0, size - 1)))
            length = (end - start) + 1 if size > 0 else 0
            with open(file_path, "rb") as f:
                f.seek(start)
                chunk = f.read(length)
            self.send_response(206)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(chunk)
            return

        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(
        self,
        status_code: int,
        content_type: str,
        data: bytes,
        attachment_filename: Optional[str] = None,
    ) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if attachment_filename:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment_filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return dict(json.loads(raw.decode("utf-8")))

    def _serve_static(self, rel_path: str) -> None:
        static_map = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "application/javascript; charset=utf-8"),
        }
        entry = static_map.get(rel_path)
        if not entry:
            self._send_json(404, {"error": f"Unknown route: {rel_path}"})
            return
        file_path = UI_DIR / entry[0]
        if not file_path.is_file():
            self._send_json(404, {"error": f"Static asset missing: {entry[0]}"})
            return
        self._send_bytes(200, entry[1], file_path.read_bytes())

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if path in {"/", "/index.html", "/styles.css", "/app.js"}:
                self._serve_static(path)
                return

            if path == "/favicon.ico":
                if brand.ICO_PATH.is_file():
                    self._send_bytes(200, "image/x-icon", brand.ICO_PATH.read_bytes())
                else:
                    self._send_json(404, {"error": "Icon not found."})
                return

            if path == "/brand/logo.png":
                if brand.LOGO_PNG_PATH.is_file():
                    self._send_bytes(200, "image/png", brand.LOGO_PNG_PATH.read_bytes())
                else:
                    self._send_json(404, {"error": "Brand logo not found."})
                return

            if path == "/brand/bg.jpg":
                if brand.BG_JPG_PATH.is_file():
                    self._send_bytes(200, "image/jpeg", brand.BG_JPG_PATH.read_bytes())
                else:
                    self._send_json(404, {"error": "Brand background not found."})
                return

            if path == "/api/about":
                self._send_json(200, brand.get_about_payload(port=self.server.server_address[1]))
                return

            if path == "/api/preflight":
                ollama_url = qs.get("ollama_url", ["http://localhost:11434"])[0]
                self._send_json(200, session_manager.get_preflight_status(ollama_url=ollama_url))
                return

            if path == "/api/status":
                self._send_json(200, self.server.controller.get_status())
                return

            if path == "/api/sessions":
                self._send_json(200, session_manager.list_all_sessions())
                return

            if path == "/api/sessions/detail":
                session_path = qs.get("path", [""])[0]
                detail = session_manager.get_session_detail(Path(session_path))
                self._send_json(200, detail)
                return

            if path == "/api/tts/voices":
                self._send_json(200, tts_engine.list_available_voices())
                return

            if path == "/api/avatars":
                avatars = presenter_engine.list_avatars()
                for item in avatars:
                    item["image_url"] = f"/api/avatars/image?file={quote(item['file'])}"
                self._send_json(200, {"avatars": avatars})
                return

            if path == "/api/avatars/image":
                fname = Path(qs.get("file", [""])[0]).name
                target = (presenter_engine.AVATARS_DIR / fname).resolve()
                if not target.is_relative_to(presenter_engine.AVATARS_DIR.resolve()) or not target.is_file():
                    self._send_json(404, {"error": "Avatar image not found."})
                    return
                self._send_bytes(200, "image/png", target.read_bytes())
                return

            if path == "/api/official-guide":
                sop_dir = session_manager.WORKFLOWS_DIR / "00_how_to_use_labguide_studio"
                has_db = (sop_dir / "session.db").is_file()
                has_html = (sop_dir / "guide.html").is_file()
                has_video = (sop_dir / "walkthrough.mp4").is_file()
                q_sop = quote(str(sop_dir))
                self._send_json(
                    200,
                    {
                        "available": has_db and has_html,
                        "path": str(sop_dir),
                        "html_url": f"/api/sessions/html?path={q_sop}" if has_html else "",
                        "video_url": f"/api/sessions/asset?session={q_sop}&file=walkthrough.mp4" if has_video else "",
                        "docx_url": f"/api/sessions/export?path={q_sop}&format=docx" if has_db else "",
                        "has_video": has_video,
                    },
                )
                return

            if path == "/api/sessions/html":
                session_path = qs.get("path", [""])[0]
                resolved = session_manager.resolve_safe_session_dir(session_path)
                html_file = resolved / "guide.html"
                if not html_file.is_file():
                    self._send_json(404, {"error": "guide.html not compiled yet."})
                    return
                html_text = html_file.read_text(encoding="utf-8")
                if 'src="./walkthrough.mp4"' in html_text:
                    video_url = f"/api/sessions/asset?session={quote(str(resolved))}&file=walkthrough.mp4"
                    html_text = html_text.replace('src="./walkthrough.mp4"', f'src="{video_url}"')
                self._send_bytes(200, "text/html; charset=utf-8", html_text.encode("utf-8"))
                return

            if path == "/api/sessions/export":
                session_path = Path(qs.get("path", [""])[0])
                fmt = qs.get("format", ["md"])[0].lower()
                inline = qs.get("inline", ["0"])[0] == "1"

                if fmt == "md":
                    data, ctype, fname = exporter.export_markdown(session_path, embed_base64=False)
                elif fmt == "md_b64":
                    data, ctype, fname = exporter.export_markdown(session_path, embed_base64=True)
                elif fmt == "html":
                    data, ctype, fname = exporter.export_html(session_path)
                elif fmt == "pdf_print":
                    data = exporter.export_pdf_print_html(session_path)
                    self._send_bytes(200, "text/html; charset=utf-8", data)
                    return
                elif fmt == "docx":
                    data, ctype, fname = exporter.export_docx(session_path)
                elif fmt == "json":
                    data, ctype, fname = exporter.export_json_bundle(session_path)
                elif fmt == "replay_py":
                    data, ctype, fname = exporter.export_replay_script(session_path)
                elif fmt == "srt":
                    data, ctype, fname = exporter.export_srt(session_path)
                elif fmt == "offline_zip":
                    data, ctype, fname = exporter.export_offline_bundle_zip(session_path)
                elif fmt == "scorm":
                    data, ctype, fname = exporter.export_scorm12_zip(session_path)
                else:
                    self._send_json(400, {"error": f"Unsupported export format: {fmt}"})
                    return

                self._send_bytes(200, ctype, data, attachment_filename=None if inline else fname)
                return

            if path == "/api/sessions/asset":
                session_path = qs.get("session", [""])[0]
                file_rel = qs.get("file", [""])[0]
                resolved = session_manager.resolve_safe_session_dir(session_path)
                assets_root = (resolved / "assets").resolve()
                audio_root = (assets_root / "audio").resolve()
                target = (resolved / file_rel).resolve()

                ext = target.suffix.lower()
                is_allowed_png = target.is_relative_to(assets_root) and ext == ".png"
                is_allowed_mp3 = target.is_relative_to(audio_root) and ext == ".mp3"
                is_allowed_mp4 = target == (resolved / "walkthrough.mp4").resolve()
                is_allowed_srt = target == (resolved / "walkthrough.srt").resolve()

                if not (is_allowed_png or is_allowed_mp3 or is_allowed_mp4 or is_allowed_srt):
                    self._send_json(403, {"error": "Forbidden asset path."})
                    return
                if not target.is_file():
                    self._send_json(404, {"error": "Asset not found."})
                    return
                ctype_map = {".png": "image/png", ".mp3": "audio/mpeg", ".mp4": "video/mp4", ".srt": "text/plain; charset=utf-8"}
                ctype = ctype_map.get(ext) or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                if ext in {".mp3", ".mp4"}:
                    self._send_file_range(target, ctype)
                else:
                    self._send_bytes(200, ctype, target.read_bytes())
                return

            self._send_json(404, {"error": f"Unknown API endpoint: {path}"})
        except FileNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})

    def do_POST(self) -> None:
        if not self._is_local_origin():
            self._send_json(403, {"error": "Forbidden cross-origin POST request."})
            return

        path = urlparse(self.path).path
        try:
            data = self._read_json_body()

            if path == "/api/record/start":
                status = self.server.controller.start(
                    title=str(data.get("title") or "Standard Operating Procedure"),
                    model=str(data.get("model") or "gemma4:e4b"),
                    ollama_url=str(data.get("ollama_url") or "http://localhost:11434"),
                    redact_pii=bool(data.get("redact_pii", True)),
                    auto_compile=bool(data.get("auto_compile", False)),
                    prompt_style=str(data.get("prompt_style") or "concise"),
                    capture_shortcuts=bool(data.get("capture_shortcuts", True)),
                    countdown_seconds=int(data.get("countdown_seconds", 3)),
                    exclude_self=bool(data.get("exclude_self", True)),
                    coalesce_typing=bool(data.get("coalesce_typing", True)),
                    keep_alive=str(data.get("keep_alive", "0")),
                    show_hud=bool(data.get("show_hud", True)),
                )
                self._send_json(200, status)
                return

            if path == "/api/record/pause":
                self._send_json(200, self.server.controller.toggle_pause())
                return

            if path == "/api/record/stop":
                self._send_json(200, self.server.controller.stop())
                return

            if path == "/api/sessions/update-title":
                detail = session_manager.update_session_title(
                    Path(str(data.get("path") or "")),
                    str(data.get("title") or ""),
                )
                self._send_json(200, detail)
                return

            if path == "/api/sessions/step/update":
                detail = session_manager.update_step_text(
                    Path(str(data.get("path") or "")),
                    int(data["step_num"]),
                    str(data.get("step_title") or ""),
                    str(data.get("step_instruction") or ""),
                    str(data.get("step_note") or ""),
                )
                self._send_json(200, detail)
                return

            if path == "/api/sessions/step/move":
                detail = session_manager.move_step(
                    Path(str(data.get("path") or "")),
                    int(data["step_num"]),
                    str(data.get("direction") or ""),
                )
                self._send_json(200, detail)
                return

            if path == "/api/sessions/step/delete":
                detail = session_manager.delete_step(
                    Path(str(data.get("path") or "")),
                    int(data["step_num"]),
                )
                self._send_json(200, detail)
                return

            if path == "/api/sessions/step/blur":
                detail = session_manager.apply_manual_blur(
                    Path(str(data.get("path") or "")),
                    int(data["step_num"]),
                    str(data.get("target") or "crop"),
                    list(data.get("boxes") or []),
                    mode=str(data.get("mode") or "blur"),
                )
                self._send_json(200, detail)
                return

            if path == "/api/sessions/step/reset-image":
                detail = session_manager.reset_step_from_raw(
                    Path(str(data.get("path") or "")),
                    int(data["step_num"]),
                    redact_pii=bool(data.get("redact_pii", False)),
                )
                self._send_json(200, detail)
                return

            if path == "/api/sessions/step/recompile":
                resolved = session_manager.resolve_safe_session_dir(str(data.get("path") or ""))
                engine = labguide_engine.LabGuideEngine(
                    session_dir=resolved,
                    title=None,
                    ollama_url=str(data.get("ollama_url") or "http://localhost:11434"),
                    model=str(data.get("model") or "gemma4:e4b"),
                    redact_pii=False,
                    prompt_style=str(data.get("prompt_style") or "concise"),
                    session_status=None,
                    keep_alive=str(data.get("keep_alive", "0")),
                )
                engine.compile_single_step(int(data["step_num"]))
                self._send_json(200, session_manager.get_session_detail(resolved))
                return

            if path == "/api/sessions/compile":
                resolved = session_manager.resolve_safe_session_dir(str(data.get("path") or ""))
                engine = labguide_engine.LabGuideEngine(
                    session_dir=resolved,
                    title=None,
                    ollama_url=str(data.get("ollama_url") or "http://localhost:11434"),
                    model=str(data.get("model") or "gemma4:e4b"),
                    redact_pii=False,
                    prompt_style=str(data.get("prompt_style") or "concise"),
                    session_status=None,
                    keep_alive=str(data.get("keep_alive", "0")),
                )
                engine.compile_guide(use_ollama=bool(data.get("use_ollama", True)))
                self._send_json(200, session_manager.get_session_detail(resolved))
                return

            if path == "/api/sessions/save":
                result = session_manager.save_session_to_workflows(
                    Path(str(data.get("path") or "")),
                    preset_key=str(data.get("preset") or "workflows"),
                    custom_name=str(data.get("custom_name") or ""),
                    keep_staging=bool(data.get("keep_staging", False)),
                )
                self._send_json(200, result)
                return

            if path == "/api/sessions/discard":
                session_manager.discard_session(Path(str(data.get("path") or "")))
                self._send_json(200, {"discarded": True, "sessions": session_manager.list_all_sessions()})
                return

            if path == "/api/open-path":
                opened = session_manager.open_in_os(
                    str(data.get("path") or ""),
                    reveal=bool(data.get("reveal", False)),
                )
                self._send_json(200, {"opened": str(opened)})
                return

            if path == "/api/sop/import":
                raw_text = str(data.get("raw_text") or "")
                file_name = str(data.get("file_name") or "")
                file_b64 = str(data.get("file_b64") or "")
                if file_b64:
                    raw_bytes = base64.b64decode(file_b64)
                    extracted = sop_importer.extract_text_from_upload(file_name or "sop.txt", raw_bytes)
                    raw_text = f"{raw_text}\n\n{extracted}".strip() if raw_text else extracted
                if not raw_text.strip():
                    raise ValueError("Provide SOP text or upload a .md, .docx, .pdf, or .txt document.")
                detail = sop_importer.import_electronic_sop(
                    raw_text=raw_text,
                    title=str(data.get("title") or "Electronic SOP"),
                    use_ollama=bool(data.get("use_ollama", True)),
                    model=str(data.get("model") or "gemma4:e4b"),
                    ollama_url=str(data.get("ollama_url") or "http://localhost:11434"),
                    doc_id=str(data.get("doc_id") or ""),
                    owner=str(data.get("owner") or brand.PUBLISHER),
                )
                self._send_json(200, detail)
                return

            if path == "/api/avatars/upload":
                img_b64 = str(data.get("image_b64") or "")
                if not img_b64:
                    raise ValueError("image_b64 is required.")
                entry = presenter_engine.register_custom_avatar(
                    name=str(data.get("name") or ""),
                    role_title=str(data.get("role_title") or "Custom Studio Presenter"),
                    image_bytes=base64.b64decode(img_b64),
                    ext=str(data.get("ext") or ".png"),
                    consent_confirmed=bool(data.get("consent_confirmed", False)),
                )
                self._send_json(200, entry)
                return

            if path == "/api/sessions/render-video":
                res = video_studio.render_session_video_sop(
                    Path(str(data.get("path") or "")),
                    voice=str(data.get("voice") or "af_heart"),
                    speed=float(data.get("speed", 1.0)),
                    presenter_mode=str(data.get("presenter_mode") or "none"),
                    avatar_id=str(data.get("avatar_id") or "aria_architect"),
                    consent_confirmed=bool(data.get("consent_confirmed", False)),
                    zoom_to_click=bool(data.get("zoom_to_click", True)),
                    burn_captions=bool(data.get("burn_captions", True)),
                )
                self._send_json(200, res)
                return

            if path == "/api/sessions/step/replace-image":
                img_b64 = str(data.get("image_b64") or "")
                if not img_b64:
                    raise ValueError("image_b64 is required.")
                detail = session_manager.replace_step_image(
                    Path(str(data.get("path") or "")),
                    int(data["step_num"]),
                    base64.b64decode(img_b64),
                )
                self._send_json(200, detail)
                return

            if path == "/api/sessions/step/rerender":
                res = video_studio.rerender_single_step(
                    Path(str(data.get("path") or "")),
                    int(data["step_num"]),
                    voice=data.get("voice"),
                    speed=float(data["speed"]) if data.get("speed") is not None else None,
                    presenter_mode=data.get("presenter_mode"),
                    avatar_id=data.get("avatar_id"),
                    consent_confirmed=data.get("consent_confirmed"),
                    zoom_to_click=data.get("zoom_to_click"),
                    burn_captions=data.get("burn_captions"),
                )
                self._send_json(200, res)
                return

            if path == "/api/sessions/iso/update":
                detail = session_manager.update_iso_metadata(
                    Path(str(data.get("path") or "")),
                    doc_id=str(data.get("doc_id") or ""),
                    version=str(data.get("version") or "1.0"),
                    owner=str(data.get("owner") or ""),
                    effective_date=str(data.get("effective_date") or ""),
                    prerequisites=str(data.get("prerequisites") or ""),
                    safety_warnings=str(data.get("safety_warnings") or ""),
                )
                self._send_json(200, detail)
                return

            if path == "/api/sessions/quiz/generate":
                detail = session_manager.generate_session_quiz(
                    Path(str(data.get("path") or "")),
                    use_ollama=bool(data.get("use_ollama", True)),
                    model=str(data.get("model") or "gemma4:e4b"),
                    ollama_url=str(data.get("ollama_url") or "http://localhost:11434"),
                )
                self._send_json(200, detail)
                return

            self._send_json(404, {"error": f"Unknown API endpoint: {path}"})
        except FileNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except (ValueError, KeyError, RuntimeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})


def run_server(host: str = "127.0.0.1", port: int = 8897, open_browser: bool = False) -> None:
    labguide_engine.enable_windows_dpi_awareness()
    session_manager.ensure_directories()
    server = LabGuideHTTPServer((host, port), LabGuideHTTPHandler)
    url = f"http://{host}:{port}"
    print(f"=== {brand.APP_NAME} v{brand.APP_VERSION} ({brand.PUBLISHER}) running at {url} ===")
    print(f"Staging folder : {session_manager.STAGING_DIR}")
    print(f"Saved workflows: {session_manager.WORKFLOWS_DIR}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[LabGuide UI] Shutting down...")
    finally:
        if server.controller._hud is not None:
            server.controller._hud.shutdown()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{brand.APP_NAME} Web UI Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8897, help="Port number (default 8897)")
    parser.add_argument("--open", action="store_true", help="Open browser automatically")
    args = parser.parse_args()
    run_server(host=args.host, port=args.port, open_browser=args.open)


if __name__ == "__main__":
    main()
