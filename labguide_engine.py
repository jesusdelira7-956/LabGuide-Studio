from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import html
import json
import queue
import re
import sqlite3
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    import mss
except ImportError:
    mss = None  # type: ignore[assignment]

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFilter = None  # type: ignore[assignment]

try:
    from pynput import keyboard, mouse
except ImportError:
    keyboard = None  # type: ignore[assignment]
    mouse = None  # type: ignore[assignment]

try:
    import uiautomation as auto
except ImportError:
    auto = None

try:
    from rapidocr_onnxruntime import RapidOCR
except ImportError:
    RapidOCR = None


def enable_windows_dpi_awareness() -> None:
    """Enforce Windows 11 Per-Monitor V2 DPI awareness so click coordinates match pixels."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


@contextlib.contextmanager
def connect_sqlite(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection that commits/rolls back AND deterministically closes on exit."""
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


PII_PATTERNS = [
    re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"),  # Emails
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),  # IPv4 addresses
    re.compile(r"\b(?:sk-|AIza|ghp_|xoxb-)[A-Za-z0-9_\-]{12,}\b"),  # API keys / tokens
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN format
]


def extract_key_char(key: Any) -> str:
    """Normalize a pynput key event into a lowercase ASCII letter or character.

    On Windows, holding Ctrl transforms key.char into ASCII control codes
    (e.g., '\\x10' for Ctrl+P, '\\x13' for Ctrl+S). Prefer virtual-key codes
    for A-Z and map control codes back to 'a'-'z'.
    """
    vk = getattr(key, "vk", None)
    if isinstance(vk, int) and 65 <= vk <= 90:
        return chr(vk).lower()

    char = getattr(key, "char", None)
    if isinstance(char, str) and len(char) == 1:
        code = ord(char)
        if 1 <= code <= 26:
            return chr(code + 96)
        return char.lower()

    return ""


@dataclass
class RawEvent:
    """Raw input event queued from non-blocking OS hook callbacks."""

    event_type: str  # "click" | "shortcut"
    timestamp: float
    x: int = 0
    y: int = 0
    button: str = ""
    shortcut: str = ""


DEFAULT_EXCLUDED_TITLES = (
    "labguide studio",
    "labguide studio mini-hud",
    "127.0.0.1:8897",
    "localhost:8897",
)

SENSITIVE_FIELD_KEYWORDS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "api key",
    "passcode",
    "pin",
)


class LabGuideEngine:
    """Multi-threaded Windows 11 workflow capture and local VLM compilation engine."""

    def __init__(
        self,
        session_dir: Path,
        title: Optional[str] = "Standard Operating Procedure",
        ollama_url: str = "http://localhost:11434",
        model: str = "gemma4:e4b",
        redact_pii: bool = True,
        auto_compile: bool = True,
        prompt_style: str = "concise",
        capture_shortcuts: bool = True,
        session_status: Optional[str] = "staging",
        exclude_self: bool = True,
        excluded_titles: tuple[str, ...] = DEFAULT_EXCLUDED_TITLES,
        coalesce_typing: bool = True,
        keep_alive: str = "0",
        on_step_captured: Optional[Callable[[int, str], None]] = None,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.assets_dir = self.session_dir / "assets"
        self.raw_dir = self.assets_dir / "raw"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.session_dir / "session.db"
        self._explicit_title = title
        self.title = title or "Standard Operating Procedure"
        self._auto_title_pending = bool(
            title is not None and title.strip() == "Standard Operating Procedure"
        )
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self.redact_pii = redact_pii
        self.auto_compile = auto_compile
        self.prompt_style = prompt_style if prompt_style in {"concise", "detailed", "troubleshooting"} else "concise"
        self.capture_shortcuts = capture_shortcuts
        self._session_status = session_status
        self.exclude_self = exclude_self
        self.excluded_titles = tuple(t.lower() for t in excluded_titles)
        self.coalesce_typing = coalesce_typing
        self.keep_alive = keep_alive
        self.on_step_captured = on_step_captured

        self.event_queue: queue.Queue[Optional[RawEvent]] = queue.Queue()
        self.paused = threading.Event()
        self.stop_event = threading.Event()
        self.step_counter = 0
        self.last_click_time = 0.0
        self.last_click_pos = (0, 0)

        self.is_recording = False
        self.is_compiling = False
        self._finalized = False
        self._lifecycle_lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self._m_listener: Any = None
        self._k_listener: Any = None
        self._on_auto_stop: Optional[Callable[[], None]] = None

        self.ocr_engine = RapidOCR() if (redact_pii and RapidOCR is not None) else None
        self._init_db()

    def _init_db(self) -> None:
        with connect_sqlite(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS steps (
                    step_num INTEGER PRIMARY KEY,
                    timestamp REAL,
                    event_type TEXT,
                    x INTEGER,
                    y INTEGER,
                    window_title TEXT,
                    control_name TEXT,
                    control_type TEXT,
                    uia_json TEXT,
                    full_image TEXT,
                    crop_image TEXT,
                    step_title TEXT,
                    step_instruction TEXT,
                    step_note TEXT
                )
                """
            )
            if self._explicit_title is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                    ("title", self._explicit_title),
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO metadata (key, value) VALUES (?, ?)",
                    ("title", self.title),
                )
            conn.execute(
                "INSERT OR IGNORE INTO metadata (key, value) VALUES (?, ?)",
                ("created_at", datetime.now().isoformat()),
            )
            if self._session_status is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO metadata (key, value) VALUES (?, ?)",
                    ("status", self._session_status),
                )

    def _get_win32_root_title(self, x: int, y: int) -> str:
        if sys.platform != "win32":
            return ""
        try:
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
            if not hwnd:
                return ""
            root_hwnd = user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT = 2
            length = user32.GetWindowTextLengthW(root_hwnd)
            if length <= 0:
                return ""
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(root_hwnd, buf, length + 1)
            return buf.value.strip()
        except Exception:
            return ""

    def _is_excluded_window(self, window_title: str, control_name: str = "") -> bool:
        if not self.exclude_self:
            return False
        haystack = f"{window_title} {control_name}".lower()
        return any(needle in haystack for needle in self.excluded_titles)

    def _apply_smart_auto_title(self, window_title: str) -> None:
        if not self._auto_title_pending:
            return
        clean = (window_title or "").strip()
        if not clean or clean in {"Unknown Window", "Active Application", "Active Window"}:
            return
        parts = [p.strip() for p in clean.split(" - ") if p.strip()]
        app_label = (parts[-1] if parts else clean)[:38].strip()
        if not app_label:
            return
        new_title = f"SOP - {app_label} ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
        self.title = new_title
        self._auto_title_pending = False
        with connect_sqlite(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                ("title", new_title),
            )

    def _query_uia_at_point(self, x: int, y: int) -> dict[str, Any]:
        info: dict[str, Any] = {
            "window_title": "Unknown Window",
            "control_name": "",
            "control_type": "",
            "automation_id": "",
            "rect": None,
        }
        if auto is not None:
            try:
                ctrl = auto.ControlFromPoint(x, y)
                if ctrl:
                    info["control_name"] = (ctrl.Name or "").strip()
                    info["control_type"] = (ctrl.ControlTypeName or "").replace("Control", "")
                    info["automation_id"] = (ctrl.AutomationId or "").strip()
                    rect = ctrl.BoundingRectangle
                    if rect and rect.width() > 0 and rect.height() > 0:
                        info["rect"] = [rect.left, rect.top, rect.right, rect.bottom]
                    top_win = ctrl.GetTopLevelControl()
                    if top_win and top_win.Name:
                        info["window_title"] = top_win.Name.strip()
            except Exception as exc:
                info["error"] = str(exc)

        if info["window_title"] == "Unknown Window":
            win32_title = self._get_win32_root_title(x, y)
            if win32_title:
                info["window_title"] = win32_title
        return info

    def _query_focused_uia(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "window_title": "Active Application",
            "control_name": "",
            "control_type": "Edit",
            "automation_id": "",
            "rect": None,
            "is_password": False,
        }
        if auto is None:
            return info
        try:
            ctrl = auto.GetFocusedControl()
            if not ctrl:
                return info
            info["control_name"] = (ctrl.Name or "").strip()
            info["control_type"] = (ctrl.ControlTypeName or "").replace("Control", "") or "Edit"
            info["automation_id"] = (ctrl.AutomationId or "").strip()
            rect = ctrl.BoundingRectangle
            if rect and rect.width() > 0 and rect.height() > 0:
                info["rect"] = [rect.left, rect.top, rect.right, rect.bottom]
            top_win = ctrl.GetTopLevelControl()
            if top_win and top_win.Name:
                info["window_title"] = top_win.Name.strip()

            is_pw = False
            try:
                is_pw = bool(getattr(ctrl, "IsPassword", False))
            except Exception:
                pass
            if not is_pw:
                try:
                    is_pw = bool(ctrl.GetPropertyValue(30019))
                except Exception:
                    pass
            combined = f"{info['control_name']} {info['automation_id']}".lower()
            if not is_pw and any(kw in combined for kw in SENSITIVE_FIELD_KEYWORDS):
                is_pw = True
            info["is_password"] = is_pw
        except Exception as exc:
            info["error"] = str(exc)
        return info

    def _apply_pii_blur(self, img: Any) -> Any:
        if not self.ocr_engine or ImageFilter is None or np is None:
            return img
        try:
            result, _ = self.ocr_engine(np.array(img))
            if not result:
                return img
            redacted = img.copy()
            for box, text, _score in result:
                if any(pat.search(text) for pat in PII_PATTERNS):
                    xs = [int(pt[0]) for pt in box]
                    ys = [int(pt[1]) for pt in box]
                    pad = 4
                    region = (
                        max(0, min(xs) - pad),
                        max(0, min(ys) - pad),
                        min(redacted.width, max(xs) + pad),
                        min(redacted.height, max(ys) + pad),
                    )
                    if region[2] > region[0] and region[3] > region[1]:
                        cropped = redacted.crop(region).filter(ImageFilter.GaussianBlur(radius=12))
                        redacted.paste(cropped, region)
            return redacted
        except Exception:
            return img

    def _capture_and_annotate(
        self, sct: Any, step_num: int, x: int, y: int, rect: Optional[list[int]]
    ) -> tuple[str, str]:
        if Image is None or ImageDraw is None:
            raise RuntimeError(
                "Pillow is required for image capture and annotation. "
                "Install dependencies via: pip install -r projects/labguide_studio/requirements.txt"
            )

        monitors = sct.monitors[1:] if len(sct.monitors) > 1 else sct.monitors
        target_mon = monitors[0]
        for mon in monitors:
            if (
                mon["left"] <= x < mon["left"] + mon["width"]
                and mon["top"] <= y < mon["top"] + mon["height"]
            ):
                target_mon = mon
                break

        shot = sct.grab(target_mon)
        full_img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        # Save untouched raw capture for future reprocessing
        raw_name = f"step_{step_num:03d}_raw.png"
        full_img.save(self.raw_dir / raw_name, "PNG")

        # Redact PII before drawing visual markers
        clean_img = self._apply_pii_blur(full_img)

        rel_x = x - target_mon["left"]
        rel_y = y - target_mon["top"]

        # Create tight context crop around clicked element or cursor
        if rect:
            c_left = max(0, (rect[0] - target_mon["left"]) - 140)
            c_top = max(0, (rect[1] - target_mon["top"]) - 90)
            c_right = min(clean_img.width, (rect[2] - target_mon["left"]) + 140)
            c_bottom = min(clean_img.height, (rect[3] - target_mon["top"]) + 90)
        else:
            c_left = max(0, rel_x - 260)
            c_top = max(0, rel_y - 160)
            c_right = min(clean_img.width, rel_x + 260)
            c_bottom = min(clean_img.height, rel_y + 160)

        crop_img = clean_img.crop((c_left, c_top, c_right, c_bottom)).copy()

        # Draw target indicator ring on full screenshot
        annotated = clean_img.copy()
        draw = ImageDraw.Draw(annotated)
        draw.ellipse((rel_x - 22, rel_y - 22, rel_x + 22, rel_y + 22), outline="#FF3B30", width=4)
        draw.ellipse((rel_x - 6, rel_y - 6, rel_x + 6, rel_y + 6), fill="#FF3B30")

        # Draw target indicator ring on cropped screenshot
        crop_draw = ImageDraw.Draw(crop_img)
        cx, cy = rel_x - c_left, rel_y - c_top
        crop_draw.ellipse((cx - 18, cy - 18, cx + 18, cy + 18), outline="#FF3B30", width=3)

        full_rel = f"assets/step_{step_num:03d}_full.png"
        crop_rel = f"assets/step_{step_num:03d}_crop.png"
        annotated.save(self.session_dir / full_rel, "PNG", optimize=True)
        crop_img.save(self.session_dir / crop_rel, "PNG", optimize=True)
        return full_rel, crop_rel

    def _select_monitor(self, sct: Any, x: int, y: int) -> dict[str, int]:
        monitors = sct.monitors[1:] if len(sct.monitors) > 1 else sct.monitors
        target_mon = monitors[0]
        for mon in monitors:
            if (
                mon["left"] <= x < mon["left"] + mon["width"]
                and mon["top"] <= y < mon["top"] + mon["height"]
            ):
                target_mon = mon
                break
        return {
            "left": int(target_mon["left"]),
            "top": int(target_mon["top"]),
            "width": int(target_mon["width"]),
            "height": int(target_mon["height"]),
        }

    def _record_step(
        self,
        sct: Any,
        event_type: str,
        timestamp: float,
        x: int,
        y: int,
        uia: dict[str, Any],
        blur_rect: bool = False,
    ) -> None:
        self.step_counter += 1
        step_num = self.step_counter
        mon = self._select_monitor(sct, x, y)
        uia["monitor"] = mon

        self._apply_smart_auto_title(uia.get("window_title", ""))
        full_rel, crop_rel = self._capture_and_annotate(
            sct, step_num, x, y, uia.get("rect")
        )

        if blur_rect and uia.get("rect") and Image is not None and ImageFilter is not None:
            try:
                rect = uia["rect"]
                full_path = self.session_dir / full_rel
                with Image.open(full_path) as im:
                    rgb = im.convert("RGB")
                region = (
                    max(0, int(rect[0]) - mon["left"]),
                    max(0, int(rect[1]) - mon["top"]),
                    min(rgb.width, int(rect[2]) - mon["left"]),
                    min(rgb.height, int(rect[3]) - mon["top"]),
                )
                if region[2] > region[0] and region[3] > region[1]:
                    patch = rgb.crop(region).filter(ImageFilter.GaussianBlur(radius=14))
                    rgb.paste(patch, region)
                    rgb.save(full_path, "PNG", optimize=True)
            except Exception:
                pass

        with connect_sqlite(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO steps (
                    step_num, timestamp, event_type, x, y,
                    window_title, control_name, control_type, uia_json,
                    full_image, crop_image
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step_num,
                    timestamp,
                    event_type,
                    x,
                    y,
                    uia.get("window_title", ""),
                    uia.get("control_name", ""),
                    uia.get("control_type", ""),
                    json.dumps(uia),
                    full_rel,
                    crop_rel,
                ),
            )
        label = uia.get("control_name") or f"({x}, {y})"
        print(
            f"[Step {step_num:02d}] Captured {event_type}: {label} "
            f"in '{uia.get('window_title')}'"
        )
        if self.on_step_captured is not None:
            try:
                self.on_step_captured(step_num, label)
            except Exception:
                pass

    def _worker_loop(self) -> None:
        if mss is None:
            raise RuntimeError(
                "mss is required for screen capture. "
                "Install dependencies via: pip install -r projects/labguide_studio/requirements.txt"
            )
        ctx = auto.UIAutomationInitializerInThread() if auto else None
        mss_factory = getattr(mss, "MSS", None) or mss.mss

        buf_chars: list[str] = []
        buf_uia: Optional[dict[str, Any]] = None
        buf_ts = 0.0
        buf_last_ts = 0.0

        try:
            with mss_factory() as sct:
                def flush_typing() -> None:
                    nonlocal buf_chars, buf_uia, buf_ts, buf_last_ts
                    if not buf_chars:
                        buf_uia = None
                        return
                    raw_text = "".join(buf_chars)
                    uia_snapshot = buf_uia or self._query_focused_uia()
                    ts_snapshot = buf_ts or time.time()
                    buf_chars = []
                    buf_uia = None
                    buf_ts = 0.0
                    buf_last_ts = 0.0

                    if not raw_text.strip():
                        return
                    if self._is_excluded_window(
                        uia_snapshot.get("window_title", ""),
                        uia_snapshot.get("control_name", ""),
                    ):
                        return

                    is_sensitive = bool(uia_snapshot.get("is_password")) or (
                        self.redact_pii and any(p.search(raw_text) for p in PII_PATTERNS)
                    )
                    display_text = "••••••••" if is_sensitive else raw_text
                    uia_snapshot["typed_text"] = display_text
                    uia_snapshot["is_sensitive"] = is_sensitive
                    if not uia_snapshot.get("control_name"):
                        uia_snapshot["control_name"] = "Input Field"

                    rect = uia_snapshot.get("rect")
                    if rect and len(rect) == 4:
                        tx = (int(rect[0]) + int(rect[2])) // 2
                        ty = (int(rect[1]) + int(rect[3])) // 2
                    else:
                        tx = self.last_click_pos[0] or 400
                        ty = self.last_click_pos[1] or 400

                    self._record_step(
                        sct,
                        "type",
                        ts_snapshot,
                        tx,
                        ty,
                        uia_snapshot,
                        blur_rect=is_sensitive,
                    )

                while not self.stop_event.is_set():
                    try:
                        event = self.event_queue.get(timeout=0.25)
                    except queue.Empty:
                        if buf_chars and (time.time() - buf_last_ts) >= 1.8:
                            flush_typing()
                        continue
                    if event is None:
                        flush_typing()
                        break
                    if self.paused.is_set():
                        continue

                    if event.event_type == "key":
                        if event.shortcut in {"<ENTER>", "<TAB>"}:
                            flush_typing()
                            continue
                        if buf_uia is None:
                            buf_uia = self._query_focused_uia()
                            buf_ts = event.timestamp
                        buf_last_ts = event.timestamp
                        if event.shortcut == "<BACKSPACE>":
                            if buf_chars:
                                buf_chars.pop()
                        else:
                            buf_chars.append(event.shortcut)
                        continue

                    if event.event_type == "click":
                        dist = abs(event.x - self.last_click_pos[0]) + abs(
                            event.y - self.last_click_pos[1]
                        )
                        if (event.timestamp - self.last_click_time) < 0.25 and dist < 8:
                            continue
                        uia = self._query_uia_at_point(event.x, event.y)
                        if self._is_excluded_window(
                            uia.get("window_title", ""), uia.get("control_name", "")
                        ):
                            continue
                        flush_typing()
                        self.last_click_time = event.timestamp
                        self.last_click_pos = (event.x, event.y)
                        self._record_step(sct, "click", event.timestamp, event.x, event.y, uia)
                    else:
                        flush_typing()
                        uia = {
                            "window_title": "Active Application",
                            "control_name": event.shortcut,
                            "control_type": "Shortcut",
                        }
                        tx = self.last_click_pos[0] or 400
                        ty = self.last_click_pos[1] or 400
                        self._record_step(sct, "shortcut", event.timestamp, tx, ty, uia)
        finally:
            if ctx is not None:
                del ctx

    def start_async(self, on_auto_stop: Optional[Callable[[], None]] = None) -> None:
        """Start OS hook listeners and worker thread without blocking the caller."""
        if mss is None or keyboard is None or mouse is None or Image is None:
            raise RuntimeError(
                "Missing recording dependencies (mss, pynput, Pillow). "
                "Install dependencies via: pip install -r projects/labguide_studio/requirements.txt"
            )

        self._on_auto_stop = on_auto_stop
        self.stop_event.clear()
        self.paused.clear()
        self._finalized = False

        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

        pressed_keys: set[Any] = set()

        def on_click(x: int, y: int, button: Any, pressed: bool) -> None:
            if pressed and button == mouse.Button.left and not self.paused.is_set():
                self.event_queue.put(RawEvent("click", time.time(), int(x), int(y), str(button)))

        def on_press(key: Any) -> Optional[bool]:
            pressed_keys.add(key)
            ctrl = keyboard.Key.ctrl_l in pressed_keys or keyboard.Key.ctrl_r in pressed_keys
            alt = (
                keyboard.Key.alt_l in pressed_keys
                or keyboard.Key.alt_r in pressed_keys
                or (hasattr(keyboard.Key, "alt_gr") and keyboard.Key.alt_gr in pressed_keys)
            )
            char = extract_key_char(key)

            if ctrl and alt and char == "p":
                self.toggle_pause()
                return None

            if ctrl and alt and char == "s":
                print("[LabGuide] Stopping recording...")
                self.stop_event.set()
                self.event_queue.put(None)
                if self._on_auto_stop is not None:
                    cb = self._on_auto_stop
                    threading.Thread(
                        target=self._auto_finalize_and_notify,
                        args=(cb,),
                        daemon=True,
                    ).start()
                return False

            # Capture meaningful shortcuts like Ctrl+S, Ctrl+C, Ctrl+V, Ctrl+Z, Ctrl+F
            if (
                self.capture_shortcuts
                and ctrl
                and char in {"s", "c", "v", "z", "f"}
                and not alt
                and not self.paused.is_set()
            ):
                self.event_queue.put(
                    RawEvent("shortcut", time.time(), shortcut=f"Ctrl+{char.upper()}")
                )
                return None

            # Coalesce printable typing & editing keys without blocking the OS hook
            if self.coalesce_typing and not ctrl and not alt and not self.paused.is_set():
                if key == keyboard.Key.backspace:
                    self.event_queue.put(RawEvent("key", time.time(), shortcut="<BACKSPACE>"))
                elif key == keyboard.Key.space:
                    self.event_queue.put(RawEvent("key", time.time(), shortcut=" "))
                elif key == keyboard.Key.enter:
                    self.event_queue.put(RawEvent("key", time.time(), shortcut="<ENTER>"))
                elif key == keyboard.Key.tab:
                    self.event_queue.put(RawEvent("key", time.time(), shortcut="<TAB>"))
                else:
                    raw_char = getattr(key, "char", None)
                    if isinstance(raw_char, str) and len(raw_char) == 1 and raw_char.isprintable():
                        self.event_queue.put(RawEvent("key", time.time(), shortcut=raw_char))
            return None

        def on_release(key: Any) -> None:
            pressed_keys.discard(key)

        self._m_listener = mouse.Listener(on_click=on_click)
        self._k_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._m_listener.start()
        self._k_listener.start()
        self.is_recording = True

    def _auto_finalize_and_notify(self, callback: Callable[[], None]) -> None:
        try:
            self.stop_and_finalize()
        finally:
            callback()

    def toggle_pause(self) -> bool:
        """Toggle pause state and return True if now paused."""
        if self.paused.is_set():
            self.paused.clear()
            print("[LabGuide] Recording RESUMED.")
            return False
        self.paused.set()
        print("[LabGuide] Recording PAUSED.")
        return True

    def stop_and_finalize(self) -> None:
        """Idempotently stop listeners, drain worker queue, and compile outputs."""
        with self._lifecycle_lock:
            if self._finalized:
                return
            self._finalized = True
            self.stop_event.set()
            self.event_queue.put(None)
            if self._m_listener is not None:
                try:
                    self._m_listener.stop()
                except Exception:
                    pass
                self._m_listener = None
            if self._k_listener is not None:
                try:
                    self._k_listener.stop()
                except Exception:
                    pass
                self._k_listener = None
            if self._worker_thread is not None:
                self._worker_thread.join(timeout=5.0)
                self._worker_thread = None
            self.is_recording = False
            self.is_compiling = True

        try:
            self.compile_guide(use_ollama=self.auto_compile)
        finally:
            self.is_compiling = False

    def record(self) -> None:
        print(f"=== LabGuide Studio Recording: '{self.title}' ===")
        print("Hotkeys: [Ctrl+Alt+P] Pause/Resume | [Ctrl+Alt+S] Stop & Compile")

        self.start_async()
        try:
            while not self.stop_event.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            self.stop_event.set()
            self.event_queue.put(None)
        finally:
            self.stop_and_finalize()

    def _post_ollama_generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.ollama_url}/api/generate"
        if requests is not None:
            resp = requests.post(url, json=payload, timeout=45)
            resp.raise_for_status()
            return dict(resp.json())

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            return dict(json.loads(resp.read().decode("utf-8")))

    def _deterministic_step_description(self, row: dict[str, Any]) -> tuple[str, str, str]:
        window_title = row.get("window_title") or "Active Window"
        control_name = row.get("control_name") or ""
        control_type = row.get("control_type") or ""
        event_type = row.get("event_type") or "click"

        if event_type == "type":
            uia = json.loads(row.get("uia_json") or "{}")
            typed_text = str(uia.get("typed_text") or "")
            is_sensitive = bool(uia.get("is_sensitive", False)) or typed_text == "••••••••"
            target_field = control_name or "input field"
            if is_sensitive:
                fallback_title = f"Enter {control_name or 'Credentials'}"
                fallback_instr = (
                    f"In {window_title}, enter your value into the {target_field} "
                    "(masked for privacy)."
                )
                return fallback_title, fallback_instr, "Sensitive input masked locally."
            fallback_title = f"Fill {control_name or 'Field'}"
            fallback_instr = f'In {window_title}, type "{typed_text}" into {target_field}.'
            return fallback_title, fallback_instr, ""

        if event_type == "shortcut":
            shortcut_label = control_name or "Keyboard Shortcut"
            fallback_title = (
                f"Press {shortcut_label}" if control_name else "Use Keyboard Shortcut"
            )
            fallback_instr = f"In {window_title}, press {shortcut_label}."
        else:
            fallback_title = (
                f"Click {control_name}" if control_name else f"Interact with {window_title}"
            )
            fallback_instr = (
                f"In {window_title}, click the {control_name} "
                f"{control_type.lower() or 'element'}."
                if control_name
                else f"Click the highlighted area inside {window_title}."
            )
        return fallback_title, fallback_instr, ""

    def _describe_step_with_ollama(self, row: dict[str, Any]) -> tuple[str, str, str]:
        window_title = row.get("window_title") or "Active Window"
        control_name = row.get("control_name") or ""
        control_type = row.get("control_type") or ""
        event_type = row.get("event_type") or "click"
        fallback_title, fallback_instr, fallback_note = self._deterministic_step_description(row)

        crop_path = self.session_dir / row["crop_image"]
        if not crop_path.exists():
            return fallback_title, fallback_instr, fallback_note

        style_guidance = {
            "concise": '  "instruction": 1 crisp imperative sentence telling the user what to do,\n  "note": optional brief tip or empty string ""',
            "detailed": '  "instruction": 2 clear tutorial sentences explaining what to click and what happens next,\n  "note": helpful context or UI orientation tip',
            "troubleshooting": '  "instruction": 1 verification-focused SOP imperative sentence stating what to check before/after clicking,\n  "note": brief pitfall warning or permission/state check',
        }.get(
            self.prompt_style,
            '  "instruction": 1 crisp imperative sentence telling the user what to do,\n  "note": optional brief tip or empty string ""',
        )

        extra_meta = ""
        if event_type == "type":
            uia = json.loads(row.get("uia_json") or "{}")
            extra_meta = f"- Typed Input: {uia.get('typed_text', '')}\n"

        img_b64 = base64.b64encode(crop_path.read_bytes()).decode("utf-8")
        prompt = (
            "You are a technical documentation writer creating a step-by-step SOP guide. "
            "Analyze the cropped UI screenshot (a red circle marks the user's exact click) "
            "and metadata:\n"
            f"- Window Title: {window_title}\n"
            f"- Event Type: {event_type}\n"
            f"- UIA Element Name: {control_name}\n"
            f"- UIA Control Type: {control_type}\n"
            f"{extra_meta}\n"
            "Return ONLY valid JSON with keys:\n"
            '  "title": short 3-6 word imperative header,\n'
            f"{style_guidance}"
        )
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": [img_b64],
            "format": "json",
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0.2},
        }

        for attempt in range(3):
            try:
                body = self._post_ollama_generate(payload)
                raw_json = body.get("response", "{}")
                parsed = json.loads(raw_json)
                title = str(parsed.get("title") or fallback_title).strip()
                instr = str(parsed.get("instruction") or fallback_instr).strip()
                note = str(parsed.get("note") or "").strip()
                return title, instr, note
            except Exception:
                time.sleep(1.5 * (attempt + 1))

        return fallback_title, fallback_instr, fallback_note

    def compile_single_step(self, step_num: int) -> Optional[dict[str, Any]]:
        """Run Ollama VLM description on a single step and regenerate guide files."""
        with connect_sqlite(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM steps WHERE step_num = ?", (int(step_num),)
            ).fetchone()
        if not row:
            return None

        row_dict = dict(row)
        s_title, s_instr, s_note = self._describe_step_with_ollama(row_dict)
        with connect_sqlite(self.db_path) as conn:
            conn.execute(
                "UPDATE steps SET step_title = ?, step_instruction = ?, step_note = ? "
                "WHERE step_num = ?",
                (s_title, s_instr, s_note, int(step_num)),
            )
            conn.row_factory = sqlite3.Row
            updated = dict(
                conn.execute("SELECT * FROM steps WHERE step_num = ?", (int(step_num),)).fetchone()
            )
        self.compile_guide(use_ollama=False)
        return updated

    def compile_guide(self, use_ollama: bool = True) -> None:
        with connect_sqlite(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            meta = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
            rows = [dict(r) for r in conn.execute("SELECT * FROM steps ORDER BY step_num ASC").fetchall()]

        guide_title = meta.get("title", self.title)
        md_path = self.session_dir / "guide.md"
        html_path = self.session_dir / "guide.html"

        if not rows:
            print("[LabGuide] No steps recorded; skipping guide generation.")
            if md_path.exists():
                md_path.write_text(f"# {guide_title}\n\n*No steps remaining.*\n", encoding="utf-8")
            if html_path.exists():
                html_path.write_text(
                    f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'/><title>{html.escape(guide_title)}</title></head>"
                    f"<body style='background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;padding:2rem;'>"
                    f"<h1>{html.escape(guide_title)}</h1><p>No steps remaining.</p></body></html>",
                    encoding="utf-8",
                )
            return

        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        mode_label = self.model if use_ollama else "local UIA / manual edit"
        print(f"[LabGuide] Generating descriptions for {len(rows)} steps using '{mode_label}'...")

        brand_logo_uri = ""
        try:
            _candidates = [Path(__file__).resolve().parent]
            if len(Path(__file__).resolve().parents) >= 5:
                _candidates.append(Path(__file__).resolve().parents[4] / "projects" / "labguide_studio")
            for _proj_dir in _candidates:
                if (_proj_dir / "brand.py").is_file():
                    if str(_proj_dir) not in sys.path:
                        sys.path.insert(0, str(_proj_dir))
                    break
            import brand as _brand

            brand_logo_uri = _brand.get_logo_data_uri(size=84)
        except Exception:
            brand_logo_uri = ""

        video_path = self.session_dir / "walkthrough.mp4"
        has_video = video_path.is_file()
        has_any_audio = any(
            (self.session_dir / "assets" / "audio" / f"step_{int(r['step_num']):03d}.mp3").is_file()
            for r in rows
        )

        md_lines = [
            f"# {guide_title}",
            f"*Generated locally on {generated_at} using `{mode_label}`*",
            "",
        ]
        if any(meta.get(k) for k in ("doc_id", "version", "owner", "effective_date")):
            md_lines.extend(
                [
                    "| Field | Value |",
                    "| :--- | :--- |",
                    f"| **Document ID** | `{meta.get('doc_id', 'SOP-DRAFT')}` |",
                    f"| **Version** | {meta.get('version', '1.0')} |",
                    f"| **Owner** | {meta.get('owner', 'Jesse in the Lab')} |",
                    f"| **Effective Date** | {meta.get('effective_date', generated_at[:10])} |",
                    "",
                ]
            )
            if meta.get("prerequisites"):
                md_lines.append(f"**Prerequisites:** {meta['prerequisites']}\n")
            if meta.get("safety_warnings"):
                md_lines.append(f"> **Security / Safety Warning:** {meta['safety_warnings']}\n")
        if has_video:
            md_lines.append("- **Narrated Video Walkthrough:** [Watch `walkthrough.mp4`](./walkthrough.mp4)")
            md_lines.append("")
        html_blocks: list[str] = []

        with connect_sqlite(self.db_path) as conn:
            for row in rows:
                if use_ollama:
                    s_title, s_instr, s_note = self._describe_step_with_ollama(row)
                    conn.execute(
                        "UPDATE steps SET step_title = ?, step_instruction = ?, step_note = ? "
                        "WHERE step_num = ?",
                        (s_title, s_instr, s_note, row["step_num"]),
                    )
                else:
                    existing_title = (row.get("step_title") or "").strip()
                    existing_instr = (row.get("step_instruction") or "").strip()
                    if existing_title and existing_instr:
                        s_title = existing_title
                        s_instr = existing_instr
                        s_note = (row.get("step_note") or "").strip()
                    else:
                        s_title, s_instr, s_note = self._deterministic_step_description(row)
                        conn.execute(
                            "UPDATE steps SET step_title = ?, step_instruction = ?, step_note = ? "
                            "WHERE step_num = ?",
                            (s_title, s_instr, s_note, row["step_num"]),
                        )

                crop_rel = Path(row["crop_image"]).as_posix()
                full_rel = Path(row["full_image"]).as_posix()
                audio_file = self.session_dir / "assets" / "audio" / f"step_{int(row['step_num']):03d}.mp3"

                md_lines.append(f"## Step {row['step_num']}: {s_title}")
                md_lines.append(s_instr)
                if s_note:
                    md_lines.append(f"> **Note:** {s_note}")
                if audio_file.is_file():
                    md_lines.append(f"- **Voice Narration:** [Listen to Step {row['step_num']}](./assets/audio/step_{int(row['step_num']):03d}.mp3)")
                md_lines.append("")
                md_lines.append(f"![Step {row['step_num']}: {s_title}](./{crop_rel})")
                md_lines.append("")
                md_lines.append("<details>")
                md_lines.append("<summary>View Full Screen Context</summary>")
                md_lines.append("")
                md_lines.append(f"![Step {row['step_num']} Full Screen](./{full_rel})")
                md_lines.append("")
                md_lines.append("</details>")
                md_lines.append("")
                md_lines.append("---")
                md_lines.append("")

                crop_path = self.session_dir / row["crop_image"]
                crop_b64 = (
                    base64.b64encode(crop_path.read_bytes()).decode("utf-8")
                    if crop_path.exists()
                    else ""
                )
                audio_html = ""
                if audio_file.is_file():
                    audio_b64 = base64.b64encode(audio_file.read_bytes()).decode("ascii")
                    audio_html = (
                        '<div class="audio-row">'
                        f'<button type="button" class="btn-listen" data-step="{row["step_num"]}">Listen to Step</button>'
                        f'<audio class="step-audio" controls preload="metadata" data-step="{row["step_num"]}" '
                        f'src="data:audio/mpeg;base64,{audio_b64}"></audio>'
                        "</div>"
                    )
                note_html = (
                    f'<div class="note"><strong>Editorial Comment:</strong> {html.escape(s_note)}</div>'
                    if s_note
                    else ""
                )
                img_html = (
                    f'<img src="data:image/png;base64,{crop_b64}" '
                    f'alt="Step {row["step_num"]}: {html.escape(s_title)}" />'
                    if crop_b64
                    else ""
                )
                html_blocks.append(
                    f"""<section class="step" data-step="{row['step_num']}" data-tts="{html.escape(f'Step {row["step_num"]}: {s_title}. {s_instr} {s_note}')}">
  <div class="step-top">
    <h2>Step {row['step_num']}: {html.escape(s_title)}</h2>
    <span class="step-badge">Step {row['step_num']} / {len(rows)}</span>
  </div>
  <p class="step-instr">{html.escape(s_instr)}</p>
  {note_html}
  {audio_html}
  {img_html}
</section>"""
                )

        md_path = self.session_dir / "guide.md"
        md_path.write_text("\n".join(md_lines), encoding="utf-8")

        tour_bar_html = ""
        tour_script_html = ""
        if has_any_audio:
            tour_bar_html = """  <div class="tour-bar" role="region" aria-label="Narrated visual tour controls">
    <div class="tour-actions">
      <button type="button" id="tour-play" class="btn-tour">Play Narrated Visual Tour</button>
      <button type="button" id="tour-prev" class="btn-sub">Prev</button>
      <button type="button" id="tour-next" class="btn-sub">Next</button>
      <label class="tour-field">Voice:
        <select id="tour-voice-mode">
          <option value="mp3" selected>Offline Embedded Voice</option>
          <option value="speech">Browser Neural Voice</option>
        </select>
      </label>
      <label class="tour-field">Speed:
        <select id="tour-speed">
          <option value="1" selected>1.0x</option>
          <option value="1.25">1.25x</option>
          <option value="1.5">1.5x</option>
        </select>
      </label>
    </div>
    <div id="tour-status" class="tour-status" aria-live="polite">Ready — click Play for an auto-advancing walkthrough.</div>
  </div>"""
            tour_script_html = """  <script>
  (() => {
    const steps = Array.from(document.querySelectorAll(".step"));
    const btnPlay = document.getElementById("tour-play");
    const btnPrev = document.getElementById("tour-prev");
    const btnNext = document.getElementById("tour-next");
    const modeSel = document.getElementById("tour-voice-mode");
    const speedSel = document.getElementById("tour-speed");
    const statusEl = document.getElementById("tour-status");
    let currentIdx = 0;
    let playingTour = false;

    function clearHighlights() {
      steps.forEach((s) => s.classList.remove("is-speaking"));
    }

    function stopAllAudio() {
      document.querySelectorAll(".step-audio").forEach((a) => {
        a.pause();
        a.currentTime = 0;
      });
      if ("speechSynthesis" in window) {
        window.speechSynthesis.cancel();
      }
    }

    function playStep(idx, autoAdvance) {
      if (idx < 0 || idx >= steps.length) {
        playingTour = false;
        btnPlay.textContent = "Play Narrated Visual Tour";
        clearHighlights();
        statusEl.textContent = "Walkthrough complete.";
        return;
      }
      currentIdx = idx;
      stopAllAudio();
      clearHighlights();
      const stepEl = steps[idx];
      stepEl.classList.add("is-speaking");
      stepEl.scrollIntoView({ behavior: "smooth", block: "center" });
      const speed = Number(speedSel.value || 1);
      statusEl.textContent = `Playing Step ${idx + 1} of ${steps.length}...`;

      const audioEl = stepEl.querySelector(".step-audio");
      if (modeSel.value === "speech" && "speechSynthesis" in window) {
        const utter = new SpeechSynthesisUtterance(stepEl.dataset.tts || "");
        utter.rate = speed;
        utter.onend = () => {
          if (autoAdvance && playingTour) playStep(idx + 1, true);
        };
        window.speechSynthesis.speak(utter);
      } else if (audioEl) {
        audioEl.playbackRate = speed;
        audioEl.onended = () => {
          if (autoAdvance && playingTour) playStep(idx + 1, true);
        };
        audioEl.play().catch(() => {});
      }
    }

    btnPlay.addEventListener("click", () => {
      if (playingTour) {
        playingTour = false;
        stopAllAudio();
        clearHighlights();
        btnPlay.textContent = "Play Narrated Visual Tour";
        statusEl.textContent = "Tour paused.";
      } else {
        playingTour = true;
        btnPlay.textContent = "Stop Tour";
        playStep(currentIdx, true);
      }
    });

    btnPrev.addEventListener("click", () => {
      playStep(Math.max(0, currentIdx - 1), playingTour);
    });
    btnNext.addEventListener("click", () => {
      playStep(Math.min(steps.length - 1, currentIdx + 1), playingTour);
    });

    document.querySelectorAll(".btn-listen").forEach((btn) => {
      btn.addEventListener("click", () => {
        const stepNum = Number(btn.dataset.step);
        const idx = steps.findIndex((s) => Number(s.dataset.step) === stepNum);
        if (idx >= 0) {
          playingTour = false;
          btnPlay.textContent = "Play Narrated Visual Tour";
          playStep(idx, false);
        }
      });
    });
  })();
  </script>"""

        iso_html = ""
        if any(meta.get(k) for k in ("doc_id", "version", "owner", "effective_date")):
            prereq_html = (
                f'<p class="iso-extra"><strong>Prerequisites:</strong> {html.escape(meta["prerequisites"])}</p>'
                if meta.get("prerequisites")
                else ""
            )
            warn_html = (
                f'<div class="iso-warn"><strong>Warning:</strong> {html.escape(meta["safety_warnings"])}</div>'
                if meta.get("safety_warnings")
                else ""
            )
            iso_html = f"""  <section class="iso-box" aria-label="Document control metadata">
    <table class="iso-table">
      <tbody>
        <tr><th>Document ID</th><td><code>{html.escape(meta.get("doc_id", "SOP-DRAFT"))}</code></td><th>Version</th><td>{html.escape(meta.get("version", "1.0"))}</td></tr>
        <tr><th>Owner</th><td>{html.escape(meta.get("owner", "Jesse in the Lab"))}</td><th>Effective Date</th><td>{html.escape(meta.get("effective_date", generated_at[:10]))}</td></tr>
      </tbody>
    </table>
    {prereq_html}
    {warn_html}
  </section>"""

        quiz_html = ""
        quiz_script_html = ""
        if meta.get("quiz_json"):
            try:
                quiz_items = json.loads(meta["quiz_json"])
            except Exception:
                quiz_items = []
            if isinstance(quiz_items, list) and quiz_items:
                q_blocks: list[str] = []
                for q_i, q in enumerate(quiz_items):
                    opts = q.get("options") or []
                    opt_labels = "".join(
                        f'<label class="quiz-opt"><input type="radio" name="q_{q_i}" value="{o_i}" /> '
                        f'<span>{html.escape(str(opt))}</span></label>'
                        for o_i, opt in enumerate(opts)
                    )
                    q_blocks.append(
                        f'<fieldset class="quiz-q" data-correct="{int(q.get("correct_index", 0))}" '
                        f'data-exp="{html.escape(str(q.get("explanation") or ""))}">'
                        f'<legend>{q_i + 1}. {html.escape(str(q.get("question") or ""))}</legend>'
                        f"{opt_labels}"
                        '<div class="quiz-feedback" hidden></div>'
                        "</fieldset>"
                    )
                quiz_html = f"""  <section class="quiz-card" aria-labelledby="quiz-heading">
    <h2 id="quiz-heading">Knowledge Check &amp; Verification</h2>
    <p class="meta">Complete the verification check below to record your comprehension score.</p>
    {"".join(q_blocks)}
    <div class="quiz-actions">
      <button type="button" id="btn-grade-quiz" class="btn-tour">Check Answers</button>
      <span id="quiz-score" class="tour-status" aria-live="polite"></span>
    </div>
  </section>"""
                quiz_script_html = """  <script>
  (() => {
    const btn = document.getElementById("btn-grade-quiz");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const qs = Array.from(document.querySelectorAll(".quiz-q"));
      let correct = 0;
      for (const fs of qs) {
        const expected = Number(fs.dataset.correct || 0);
        const checked = fs.querySelector("input[type='radio']:checked");
        const fb = fs.querySelector(".quiz-feedback");
        fb.hidden = false;
        if (checked && Number(checked.value) === expected) {
          correct += 1;
          fb.className = "quiz-feedback ok";
          fb.textContent = "Correct! " + (fs.dataset.exp || "");
        } else {
          fb.className = "quiz-feedback err";
          fb.textContent = "Review this step: " + (fs.dataset.exp || "");
        }
      }
      const pct = qs.length ? Math.round((correct / qs.length) * 100) : 100;
      document.getElementById("quiz-score").textContent = `Score: ${correct}/${qs.length} (${pct}%)`;
      if (typeof window.labGuideReportScormScore === "function") {
        window.labGuideReportScormScore(pct);
      }
    });
  })();
  </script>"""

        video_html = ""
        if has_video:
            video_html = """  <details class="video-drawer">
    <summary>Watch Full Narrated Video Walkthrough (MP4)</summary>
    <div class="video-wrap">
      <video controls width="820" height="512" src="./walkthrough.mp4"></video>
    </div>
  </details>"""

        brand_badge_html = (
            f'<img src="{brand_logo_uri}" alt="Jesse in the Lab" width="52" height="52" '
            'style="width:52px;height:52px;border-radius:50%;flex-shrink:0;box-shadow:0 0 12px rgba(56,189,248,0.35);border:none;margin:0;" />'
            if brand_logo_uri
            else ""
        )

        html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{html.escape(guide_title)}</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      max-width: 900px;
      margin: 2rem auto;
      padding: 0 1rem 3rem;
      background: #0f172a;
      color: #e2e8f0;
      line-height: 1.6;
    }}
    h1 {{
      color: #38bdf8;
      border-bottom: 1px solid #334155;
      padding-bottom: 0.5rem;
      margin-bottom: 0.25rem;
    }}
    .meta {{
      color: #94a3b8;
      font-size: 0.9rem;
      margin-bottom: 1.25rem;
    }}
    .iso-box, .quiz-card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 10px;
      padding: 1rem 1.25rem;
      margin-bottom: 1.25rem;
    }}
    .iso-table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
    .iso-table th, .iso-table td {{ border: 1px solid #334155; padding: 0.4rem 0.65rem; text-align: left; }}
    .iso-table th {{ color: #94a3b8; width: 18%; background: #0f172a; }}
    .iso-extra {{ margin: 0.65rem 0 0; font-size: 0.9rem; }}
    .iso-warn {{ background: rgba(245, 158, 11, 0.14); border-left: 3px solid #f59e0b; padding: 0.5rem 0.75rem; margin-top: 0.65rem; font-size: 0.9rem; }}
    .quiz-q {{ border: 1px solid #334155; border-radius: 8px; padding: 0.75rem 1rem; margin-bottom: 0.85rem; }}
    .quiz-q legend {{ font-weight: 600; padding: 0 0.35rem; }}
    .quiz-opt {{ display: flex; align-items: center; gap: 0.5rem; padding: 0.35rem 0.5rem; border-radius: 6px; cursor: pointer; margin-top: 0.25rem; }}
    .quiz-opt:has(:checked) {{ background: rgba(56, 189, 248, 0.15); border: 1px solid #38bdf8; }}
    .quiz-feedback {{ margin-top: 0.45rem; font-size: 0.88rem; padding: 0.35rem 0.6rem; border-radius: 5px; }}
    .quiz-feedback.ok {{ background: rgba(16, 185, 129, 0.15); color: #a7f3d0; }}
    .quiz-feedback.err {{ background: rgba(239, 68, 68, 0.15); color: #fecaca; }}
    .quiz-actions {{ display: flex; align-items: center; gap: 0.85rem; }}
    .tour-bar {{
      position: sticky;
      top: 0.75rem;
      z-index: 20;
      background: rgba(15, 23, 42, 0.95);
      border: 1px solid #38bdf8;
      border-radius: 10px;
      padding: 0.8rem 1rem;
      margin-bottom: 1.25rem;
      display: flex;
      flex-direction: column;
      gap: 0.45rem;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
    }}
    .tour-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.55rem;
      align-items: center;
    }}
    .btn-tour, .btn-sub, .btn-listen {{
      border: 1px solid #334155;
      border-radius: 6px;
      padding: 0.42rem 0.8rem;
      font: inherit;
      font-size: 0.88rem;
      font-weight: 600;
      cursor: pointer;
      color: #f8fafc;
      background: #1e293b;
    }}
    .btn-tour {{ background: #0284c7; border-color: #38bdf8; }}
    .btn-listen {{ background: #0369a1; border-color: #38bdf8; flex-shrink: 0; }}
    .tour-field {{
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      font-size: 0.85rem;
      color: #cbd5e1;
    }}
    .tour-field select {{
      background: #1e293b;
      color: #f8fafc;
      border: 1px solid #475569;
      border-radius: 5px;
      padding: 0.25rem 0.45rem;
    }}
    .tour-status {{
      font-size: 0.85rem;
      color: #7dd3fc;
    }}
    .video-drawer {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 10px;
      padding: 0.75rem 1rem;
      margin-bottom: 1.5rem;
    }}
    .video-drawer summary {{
      cursor: pointer;
      font-weight: 600;
      color: #38bdf8;
    }}
    .video-wrap {{
      margin-top: 0.75rem;
    }}
    video {{
      max-width: 100%;
      height: auto;
      border-radius: 8px;
      border: 1px solid #475569;
    }}
    .step {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 10px;
      padding: 1.25rem;
      margin-bottom: 1.5rem;
    }}
    .step.is-speaking {{
      border-color: #38bdf8;
      box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.35);
    }}
    .step-top {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 0.75rem;
    }}
    .step h2 {{
      margin-top: 0;
      color: #f8fafc;
      font-size: 1.15rem;
    }}
    .step-badge {{
      color: #94a3b8;
      font-size: 0.82rem;
      flex-shrink: 0;
    }}
    .note {{
      background: #0f172a;
      border-left: 3px solid #38bdf8;
      padding: 0.6rem 0.9rem;
      margin: 0.75rem 0;
      font-size: 0.95rem;
    }}
    .audio-row {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.65rem;
      margin: 0.75rem 0;
    }}
    .step-audio {{
      height: 36px;
      flex: 1 1 240px;
    }}
    img {{
      max-width: 100%;
      border-radius: 6px;
      border: 1px solid #475569;
      margin-top: 0.5rem;
      display: block;
    }}
  </style>
</head>
<body>
  <div style="display:flex;align-items:center;gap:0.9rem;border-bottom:1px solid #334155;padding-bottom:0.75rem;margin-bottom:0.5rem;">
    {brand_badge_html}
    <div>
      <h1 style="border:none;padding:0;margin:0;">{html.escape(guide_title)}</h1>
      <p class="meta" style="margin:0.25rem 0 0;">Generated locally by <strong>LabGuide Studio v3.0.0</strong> (Jesse in the Lab) on {html.escape(generated_at)} using <code>{html.escape(self.model)}</code></p>
    </div>
  </div>
{iso_html}
{tour_bar_html}
{video_html}
  {"".join(html_blocks)}
{quiz_html}
{tour_script_html}
{quiz_script_html}
</body>
</html>
"""
        html_path = self.session_dir / "guide.html"
        html_path.write_text(html_doc, encoding="utf-8")
        print(f"[LabGuide] Done! Saved:\n  - Markdown: {md_path}\n  - HTML: {html_path}")


def main() -> None:
    enable_windows_dpi_awareness()

    parser = argparse.ArgumentParser(description="LabGuide Studio Windows 11 Workflow Recorder")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="Record a new workflow session")
    rec.add_argument("--title", default="Standard Operating Procedure", help="Guide title")
    rec.add_argument("--model", default="gemma4:e4b", help="Local Ollama VLM model tag")
    rec.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama base URL")
    rec.add_argument("--no-redact", action="store_true", help="Disable local OCR PII redaction")
    rec.add_argument("--out", default="sessions", help="Base output directory")

    comp = sub.add_parser("compile", help="Re-compile an existing SQLite session")
    comp.add_argument("--session", required=True, help="Path to existing session directory")
    comp.add_argument("--title", default=None, help="Optional override title for the guide")
    comp.add_argument("--model", default="gemma4:e4b", help="Local Ollama VLM model tag")
    comp.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama base URL")
    comp.add_argument(
        "--style",
        default="concise",
        choices=["concise", "detailed", "troubleshooting"],
        help="AI instruction writing style",
    )

    ui_cmd = sub.add_parser("ui", help="Launch the LabGuide Studio Web UI")
    ui_cmd.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    ui_cmd.add_argument("--port", type=int, default=8897, help="Port number (default 8897)")
    ui_cmd.add_argument("--open", action="store_true", help="Open the Web UI in your default browser")

    args = parser.parse_args()

    if args.cmd == "record":
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_dir = Path(args.out) / session_id
        engine = LabGuideEngine(
            session_dir=session_dir,
            title=args.title,
            ollama_url=args.ollama_url,
            model=args.model,
            redact_pii=not args.no_redact,
        )
        engine.record()
    elif args.cmd == "compile":
        session_dir = Path(args.session)
        engine = LabGuideEngine(
            session_dir=session_dir,
            title=args.title,
            ollama_url=args.ollama_url,
            model=args.model,
            redact_pii=False,
            prompt_style=args.style,
            session_status=None,
        )
        engine.compile_guide()
    elif args.cmd == "ui":
        _candidates = [Path(__file__).resolve().parent]
        if len(Path(__file__).resolve().parents) >= 5:
            _candidates.append(Path(__file__).resolve().parents[4] / "projects" / "labguide_studio")
        for project_dir in _candidates:
            if (project_dir / "web_server.py").is_file():
                if str(project_dir) not in sys.path:
                    sys.path.insert(0, str(project_dir))
                break
        import web_server

        web_server.run_server(host=args.host, port=args.port, open_browser=args.open)


if __name__ == "__main__":
    main()
