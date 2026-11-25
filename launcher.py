from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Optional

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import brand  # noqa: E402

WORKSPACE_ROOT = brand.get_runtime_root()
SKILL_DIR = brand.get_skill_dir()
SCRIPTS_DIR = SKILL_DIR / "scripts" if (SKILL_DIR / "scripts").is_dir() else PROJECT_DIR
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import labguide_engine  # noqa: E402
import presenter_engine  # noqa: E402
import session_manager  # noqa: E402
import tts_engine  # noqa: E402
import web_server  # noqa: E402

REQUIRED_IMPORTS: tuple[str, ...] = (
    "mss",
    "pynput",
    "PIL",
    "uiautomation",
    "requests",
    "rapidocr_onnxruntime",
    "kokoro_onnx",
    "soundfile",
    "pypdf",
)


def set_console_title(title: str) -> None:
    """Set the Windows console window title without failing on non-console hosts."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(str(title))
    except Exception:
        pass


def is_http_alive(url: str, timeout: float = 1.5) -> bool:
    """Return True if an HTTP GET returns a 2xx/3xx response."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            return 200 <= int(status or 200) < 400
    except Exception:
        return False


def find_ollama_executable() -> Optional[Path]:
    """Locate the local Ollama binary on PATH or standard Windows install paths."""
    on_path = shutil.which("ollama")
    if on_path:
        p = Path(on_path)
        if p.is_file():
            return p

    local_app = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        local_app / "Programs" / "Ollama" / "ollama.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Ollama" / "ollama.exe",
    ]
    for cand in candidates:
        if str(cand) and cand.is_file():
            return cand
    return None


def _query_ollama_tags(ollama_url: str, timeout: float = 2.0) -> Optional[dict[str, Any]]:
    url = f"{ollama_url.rstrip('/')}/api/tags"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except Exception:
        return None


def ensure_ollama_running(
    ollama_url: str = "http://127.0.0.1:11434",
    wait_seconds: float = 12.0,
) -> dict[str, Any]:
    """Ensure local Ollama is running, starting `ollama serve` detached if installed."""
    clean_url = ollama_url.rstrip("/")
    payload = _query_ollama_tags(clean_url, timeout=1.5)
    if payload is not None:
        models = [str(m.get("name", "")) for m in payload.get("models", []) if m.get("name")]
        return {
            "online": True,
            "started_by_launcher": False,
            "executable": str(find_ollama_executable() or ""),
            "models": models,
            "has_gemma4": any("gemma4" in m.lower() for m in models),
            "url": clean_url,
            "error": "",
        }

    ollama_exe = find_ollama_executable()
    if ollama_exe is None:
        return {
            "online": False,
            "started_by_launcher": False,
            "executable": "",
            "models": [],
            "has_gemma4": False,
            "url": clean_url,
            "error": "ollama.exe not found; Studio will run in offline / 0 MB VRAM fallback mode.",
        }

    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )

    try:
        subprocess.Popen(
            [str(ollama_exe), "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except Exception as exc:
        return {
            "online": False,
            "started_by_launcher": False,
            "executable": str(ollama_exe),
            "models": [],
            "has_gemma4": False,
            "url": clean_url,
            "error": f"Failed to launch Ollama: {exc}",
        }

    deadline = time.monotonic() + max(0.5, float(wait_seconds))
    while time.monotonic() < deadline:
        payload = _query_ollama_tags(clean_url, timeout=1.5)
        if payload is not None:
            models = [str(m.get("name", "")) for m in payload.get("models", []) if m.get("name")]
            return {
                "online": True,
                "started_by_launcher": True,
                "executable": str(ollama_exe),
                "models": models,
                "has_gemma4": any("gemma4" in m.lower() for m in models),
                "url": clean_url,
                "error": "",
            }
        time.sleep(0.5)

    return {
        "online": False,
        "started_by_launcher": True,
        "executable": str(ollama_exe),
        "models": [],
        "has_gemma4": False,
        "url": clean_url,
        "error": f"Launched {ollama_exe}, but {clean_url}/api/tags did not respond within {wait_seconds:.1f}s.",
    }


def ensure_ffmpeg_on_path() -> Optional[Path]:
    """Locate FFmpeg on PATH or Windows WinGet/local paths and add its bin dir to PATH."""
    on_path = shutil.which("ffmpeg")
    if on_path:
        p = Path(on_path)
        if p.is_file():
            return p

    local_app = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates: list[Path] = [
        local_app / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ffmpeg" / "bin" / "ffmpeg.exe",
    ]
    winget_pkgs = local_app / "Microsoft" / "WinGet" / "Packages"
    if winget_pkgs.is_dir():
        for pkg_dir in winget_pkgs.glob("*FFmpeg*"):
            candidates.extend(pkg_dir.rglob("ffmpeg.exe"))

    for cand in candidates:
        if cand.is_file():
            bin_dir = str(cand.parent)
            cur_path = os.environ.get("PATH", "")
            if bin_dir.lower() not in cur_path.lower():
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{cur_path}" if cur_path else bin_dir
            return cand
    return None


def ensure_python_dependencies(auto_install: bool = True) -> dict[str, bool]:
    """Verify core Python imports and optionally pip-install missing packages from requirements.txt."""
    status = {mod: (importlib.util.find_spec(mod) is not None) for mod in REQUIRED_IMPORTS}
    if not all(status.values()) and auto_install:
        req_file = PROJECT_DIR / "requirements.txt"
        if req_file.is_file():
            print(f"[Launcher] Installing missing Python packages from {req_file}...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
                check=False,
            )
            status = {mod: (importlib.util.find_spec(mod) is not None) for mod in REQUIRED_IMPORTS}
    return status


def run_dependency_bootstrap(
    ollama_url: str = "http://127.0.0.1:11434",
    auto_install: bool = True,
) -> dict[str, Any]:
    """Run all LabGuide Studio preflight checks and start local background services."""
    print(f"=== {brand.APP_NAME} v{brand.APP_VERSION} ({brand.PUBLISHER}) Dependency Bootstrap ===")
    labguide_engine.enable_windows_dpi_awareness()
    session_manager.ensure_directories()
    avatars = presenter_engine.list_avatars()
    py_deps = ensure_python_dependencies(auto_install=auto_install)
    ollama = ensure_ollama_running(ollama_url=ollama_url)

    ffmpeg_path = ""
    ffmpeg_ready = False
    ffmpeg_error = ""
    try:
        ensure_ffmpeg_on_path()
        ffmpeg_path = str(tts_engine.find_ffmpeg_executable())
        ffmpeg_ready = True
    except Exception as exc:
        ffmpeg_error = str(exc)

    voice_info = tts_engine.list_available_voices()

    summary: dict[str, Any] = {
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
            "in_venv": sys.prefix != sys.base_prefix,
            "packages": py_deps,
            "all_ready": all(py_deps.values()),
        },
        "directories": {
            "staging": str(session_manager.STAGING_DIR),
            "workflows": str(session_manager.WORKFLOWS_DIR),
        },
        "avatars_ready": len(avatars),
        "ollama": ollama,
        "ffmpeg": {
            "ready": ffmpeg_ready,
            "path": ffmpeg_path,
            "error": ffmpeg_error,
        },
        "tts": {
            "engine": voice_info.get("engine"),
            "kokoro_ready": bool(voice_info.get("kokoro_ready", False)),
            "voice_count": len(voice_info.get("voices", [])),
        },
    }

    missing_pkgs = [k for k, ok in py_deps.items() if not ok]
    print(f"[Check] Python       : {summary['python']['version']} ({'venv' if summary['python']['in_venv'] else 'system'})")
    print(f"[Check] Packages     : {'All ready' if not missing_pkgs else 'Missing: ' + ', '.join(missing_pkgs)}")
    if ollama["online"]:
        origin = "started just now" if ollama["started_by_launcher"] else "already online"
        print(f"[Check] Ollama       : Online ({origin}, {len(ollama['models'])} models)")
    else:
        print(f"[Check] Ollama       : Offline ({ollama['error']})")
    print(f"[Check] FFmpeg       : {'Ready (' + ffmpeg_path + ')' if ffmpeg_ready else 'Missing (' + ffmpeg_error + ')'}")
    print(
        f"[Check] TTS / Avatars: engine={summary['tts']['engine']} "
        f"(kokoro_ready={summary['tts']['kokoro_ready']}, voices={summary['tts']['voice_count']}), "
        f"avatars={summary['avatars_ready']}"
    )
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=f"Start all {brand.APP_NAME} dependencies and launch the Web UI.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=brand.DEFAULT_PORT, help=f"Port number (default {brand.DEFAULT_PORT})")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="Local Ollama base URL")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    parser.add_argument("--check-only", action="store_true", help="Run dependency checks and exit cleanly")
    parser.add_argument("--no-auto-install", action="store_true", help="Skip automatic pip install of missing packages")
    args = parser.parse_args(argv)

    set_console_title(f"{brand.APP_NAME} v{brand.APP_VERSION} — {brand.PUBLISHER} (Port {args.port})")

    try:
        summary = run_dependency_bootstrap(
            ollama_url=args.ollama_url,
            auto_install=not args.no_auto_install,
        )
        if args.check_only:
            print(json.dumps(summary, indent=2))
            return 0

        url = f"http://{args.host}:{args.port}"
        if is_http_alive(f"{url}/api/preflight", timeout=1.5):
            print(f"[Launcher] {brand.APP_NAME} is already running at {url}")
            if not args.no_browser:
                webbrowser.open(url)
                time.sleep(1.5)
            return 0

        print(f"[Launcher] Starting {brand.APP_NAME} on {url}")
        print(f"Keep this window open (you can minimize it) while using {brand.APP_NAME}. Press Ctrl+C or close this window to stop.")
        web_server.run_server(
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
        return 0
    except KeyboardInterrupt:
        print("\n[Launcher] Stopped by user.")
        return 0
    except Exception as exc:
        print(f"\n[Launcher ERROR] {exc}", file=sys.stderr)
        if sys.stdin is not None and sys.stdin.isatty():
            try:
                input("\nStartup failed. Press Enter to close...")
            except EOFError:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
