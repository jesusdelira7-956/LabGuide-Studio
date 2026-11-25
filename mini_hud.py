from __future__ import annotations

import ctypes
import queue
import sys
import threading
from collections.abc import Callable
from typing import Any, Optional

try:
    import tkinter as tk
except ImportError:
    tk = None  # type: ignore[assignment]

WDA_EXCLUDEFROMCAPTURE = 0x00000011


class MiniHudManager:
    """Always-on-top, screenshot-excluded tkinter HUD pill for active recordings."""

    def __init__(
        self,
        on_pause: Callable[[], None],
        on_stop: Callable[[], None],
    ) -> None:
        self._on_pause = on_pause
        self._on_stop = on_stop
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self._unavailable = threading.Event()
        self._lock = threading.Lock()

    def _ensure_started(self) -> bool:
        if tk is None or self._unavailable.is_set():
            return False
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._started.clear()
                self._thread = threading.Thread(target=self._ui_loop, daemon=True)
                self._thread.start()
        self._started.wait(timeout=2.0)
        return not self._unavailable.is_set()

    def show(self, mode: str = "recording", step_count: int = 0, countdown: int = 0) -> None:
        if not self._ensure_started():
            return
        self._queue.put(("show", {"mode": mode, "step_count": step_count, "countdown": countdown}))

    def update(self, mode: str, step_count: int = 0, countdown: int = 0) -> None:
        if tk is None or self._unavailable.is_set():
            return
        if self._thread is not None and self._thread.is_alive():
            self._queue.put(("update", {"mode": mode, "step_count": step_count, "countdown": countdown}))

    def hide(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._queue.put(("hide", None))

    def shutdown(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._queue.put(("quit", None))

    def _apply_capture_exclusion(self, root: Any) -> None:
        if sys.platform != "win32":
            return
        try:
            root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        except Exception:
            pass

    def _ui_loop(self) -> None:
        try:
            root = tk.Tk()
        except Exception:
            self._unavailable.set()
            self._started.set()
            return

        root.title("LabGuide Studio Mini-HUD")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.94)
        except Exception:
            pass
        root.configure(bg="#0f172a")

        sw = root.winfo_screenwidth()
        root.geometry(f"+{max(16, sw - 360)}+20")

        frame = tk.Frame(
            root,
            bg="#0f172a",
            highlightbackground="#38bdf8",
            highlightthickness=1,
            padx=10,
            pady=6,
        )
        frame.pack(fill="both", expand=True)

        status_var = tk.StringVar(value="● REC")
        count_var = tk.StringVar(value="0 steps")

        status_lbl = tk.Label(
            frame,
            textvariable=status_var,
            fg="#ef4444",
            bg="#0f172a",
            font=("Segoe UI", 9, "bold"),
            cursor="fleur",
        )
        status_lbl.pack(side="left", padx=(0, 8))

        count_lbl = tk.Label(
            frame,
            textvariable=count_var,
            fg="#e2e8f0",
            bg="#0f172a",
            font=("Segoe UI", 9),
            cursor="fleur",
        )
        count_lbl.pack(side="left", padx=(0, 10))

        pause_btn = tk.Button(
            frame,
            text="Pause",
            bg="#1e293b",
            fg="#f8fafc",
            activebackground="#334155",
            activeforeground="#f8fafc",
            relief="flat",
            padx=8,
            pady=2,
            font=("Segoe UI", 8, "bold"),
            command=lambda: threading.Thread(target=self._on_pause, daemon=True).start(),
        )
        pause_btn.pack(side="left", padx=(0, 6))

        stop_btn = tk.Button(
            frame,
            text="Stop",
            bg="#b91c1c",
            fg="#ffffff",
            activebackground="#991b1b",
            activeforeground="#ffffff",
            relief="flat",
            padx=8,
            pady=2,
            font=("Segoe UI", 8, "bold"),
            command=lambda: threading.Thread(target=self._on_stop, daemon=True).start(),
        )
        stop_btn.pack(side="left")

        drag_state = {"x": 0, "y": 0}

        def start_drag(event: Any) -> None:
            drag_state["x"] = event.x
            drag_state["y"] = event.y

        def on_drag(event: Any) -> None:
            nx = root.winfo_x() + (event.x - drag_state["x"])
            ny = root.winfo_y() + (event.y - drag_state["y"])
            root.geometry(f"+{nx}+{ny}")

        for w in (frame, status_lbl, count_lbl):
            w.bind("<ButtonPress-1>", start_drag)
            w.bind("<B1-Motion>", on_drag)

        self._apply_capture_exclusion(root)
        root.withdraw()
        self._started.set()

        def apply_state(data: dict[str, Any]) -> None:
            mode = data.get("mode", "recording")
            steps = int(data.get("step_count", 0))
            countdown = int(data.get("countdown", 0))
            count_var.set(f"{steps} step" if steps == 1 else f"{steps} steps")

            if mode == "countdown":
                status_var.set(f"⏳ {countdown}s")
                status_lbl.configure(fg="#38bdf8")
                pause_btn.configure(state="disabled", text="Pause")
            elif mode == "paused":
                status_var.set("❚❚ PAUSED")
                status_lbl.configure(fg="#f59e0b")
                pause_btn.configure(state="normal", text="Resume")
            elif mode == "compiling":
                status_var.set("⚙ COMPILING")
                status_lbl.configure(fg="#38bdf8")
                pause_btn.configure(state="disabled")
            else:
                status_var.set("● REC")
                status_lbl.configure(fg="#ef4444")
                pause_btn.configure(state="normal", text="Pause")

        def poll() -> None:
            while True:
                try:
                    cmd, payload = self._queue.get_nowait()
                except queue.Empty:
                    break

                if cmd == "show":
                    apply_state(payload)
                    root.deiconify()
                    self._apply_capture_exclusion(root)
                elif cmd == "update":
                    apply_state(payload)
                elif cmd == "hide":
                    root.withdraw()
                elif cmd == "quit":
                    root.destroy()
                    return
            root.after(100, poll)

        root.after(100, poll)
        root.mainloop()
