# Changelog

All notable releases of **LabGuide Studio** by **Jesse in the Lab** are documented in this file.

## [3.0.0] - 2026-11-24

### Added
- **Official Rebrand & Ownership**: Rebranded cleanly to **LabGuide Studio** by **Jesse in the Lab** (`MIT License`), integrating the circular Jesse in the Lab emblem, multi-resolution `.ico`, glassmorphic lab-themed Web UI backdrop (`/brand/bg.jpg`) with toggleable Minimal Dark mode, `/api/about` metadata endpoint, and native About & License `<dialog>`.
- **Full-Dependency Windows Desktop Launcher** (`launcher.py`, `create_desktop_shortcut.py`, `launch_labguide.ps1`): Auto-verifies Python `.venv`, starts local `ollama serve` if offline, discovers WinGet FFmpeg on `PATH`, verifies Kokoro-82M CPU weights, and installs `LabGuide Studio.lnk` on Desktop.
- **Electronic-to-Video SOP Suite** (`sop_importer.py`, `tts_engine.py`, `presenter_engine.py`, `video_studio.py`): Import `.md`, `.txt`, `.docx`, `.pdf`, or raw text into `1920x1080` slides; synthesize neural CPU narration with `[pause Xs]` tags via Kokoro-82M; render `zoompan` + burned `.srt` + consent-gated PiP presenter `.mp4` videos; export ISO Control Headers, 3-question interactive Knowledge Check quizzes, Offline ZIP bundles, and SCORM 1.2 LMS packages.
- **Release Governance Docs**: Added `LICENSE`, `THIRD_PARTY_LICENSES.md`, `SECURITY.md`, `PRIVACY.md`, `CONTRIBUTING.md`, `brand.py`, and rebuilt official SOP walkthrough `workflows/00_how_to_use_labguide_studio`.

## [2.1.0] - 2026-11-24
- Added staging-first draft review (`sessions/_staging/`), self-click window exclusion, password-masked keystroke coalescing, screenshot-excluded Mini-HUD pill, manual privacy blur canvas, `.docx`/PDF/HTML/Markdown/`replay.py` exports, and Visual & Voice Official Guide.

## [1.0.0] - 2026-11-24
- Initial Windows 11 Per-Monitor V2 DPI-aware click/shortcut capture engine, SQLite session store, RapidOCR PII redaction, and local Ollama VLM SOP compiler.
