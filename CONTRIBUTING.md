# Contributing to LabGuide Studio

Thank you for building with **LabGuide Studio** (`Jesse in the Lab`).

## Development Environment (Windows 11)

1. Activate the workspace Python 3.11+ virtual environment:
   ```powershell
   .\.venv\Scripts\Activate.ps1
   pip install -r projects/labguide_studio/requirements.txt
   ```
2. Verify all local dependencies (Python packages, Ollama, WinGet FFmpeg, Kokoro-82M weights, and Studio avatars):
   ```powershell
   python projects/labguide_studio/launcher.py --check-only --no-auto-install
   ```

## Engineering Rules & Platform Invariants

1. **Per-Monitor V2 DPI Awareness**: Always call `labguide_engine.enable_windows_dpi_awareness()` before reading mouse coordinates or capturing frames.
2. **Non-Blocking Hooks**: Never perform disk I/O, network requests, or `uiautomation` lookups inside `pynput` callback threads; enqueue raw events onto a `queue.Queue`.
3. **COM Threading**: Wrap any background thread invoking `uiautomation` with `uiautomation.UIAutomationInitializerInThread()`.
4. **VRAM Headroom (>= 1200 MB free)**: Keep CPU workloads (Kokoro-82M, RapidOCR, Pillow, FFmpeg `libx264`) on CPU (`0 MB` VRAM) and pass `"keep_alive": "0"` on Ollama API calls so VRAM unloads immediately.
5. **Clean Resource Closing**: Close all SQLite connections (`labguide_engine.connect_sqlite`), Pillow images (`with Image.open(...)`), and HTTP responses (`with urllib.request.urlopen(...)`) deterministically.

## Verification Before Committing

Run both commands and ensure zero failures and zero warnings:

```powershell
.\.venv\Scripts\python.exe -W error::DeprecationWarning -W error::ResourceWarning -m unittest discover -s projects/labguide_studio/tests -v
```

Validate modified Python modules with the workspace AST/integrity gate:

```powershell
.\.venv\Scripts\python.exe execution/security/asset_verifier.py projects/labguide_studio/web_server.py
```
