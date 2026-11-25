# LabGuide Studio Workspace Rules

> **LabGuide Studio** by **Jesse in the Lab** (`v3.0.0` · MIT License)

## Architecture & Platform Constraints
- **Target OS**: Windows 11 (x64/ARM64). All screen-capture and coordinate scripts must enforce Per-Monitor V2 DPI awareness (`SetProcessDpiAwareness(2)`) before reading mouse coordinates or capturing frames.
- **Non-Blocking OS Hooks**: Never perform disk I/O, Windows UI Automation (UIA) tree traversal, or HTTP requests inside `pynput` callback threads. Push raw events to a thread-safe `queue.Queue` so Windows never drops the low-level hook (`LowLevelHooksTimeout`).
- **COM Thread Safety**: Any background thread calling `uiautomation` must wrap execution in `uiautomation.UIAutomationInitializerInThread()`.

## Privacy, Hardware & Local AI Standards
- **100% Local Execution**: Never send screenshots, UIA metadata, or SQLite records to external cloud APIs. Use local Ollama (`http://localhost:11434`) exclusively.
- **VRAM Headroom Guardrail**: Respect the workspace invariant of maintaining >= 1200 MB free VRAM headroom on the AMD Radeon RX 6650 XT (8 GB VRAM). Local OCR (`rapidocr-onnxruntime`), Kokoro-82M TTS, Pillow compositing, and FFmpeg `libx264` encoding run strictly on CPU (`0 MB` GPU VRAM). Always pass `"keep_alive": "0"` on Ollama vision/SOP calls.
- **SQLite Safety**: Never copy active `session.db` files with raw file copy while recording; use `labguide_engine.connect_sqlite()` or the SQLite online backup API (`AgentBackupSystem`).
- **Resilient Fallbacks**: All Ollama vision calls must use structured JSON output and fall back gracefully to deterministic UIA element descriptions if the local model server is offline.
- **Avatar Consent & Provenance**: Built-in Studio avatars are original CC0 artwork; custom presenter uploads and PiP rendering require explicit `consent_confirmed=True` logged in `assets/avatars/manifest.json`.
