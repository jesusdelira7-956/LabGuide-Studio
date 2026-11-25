# Privacy Guarantee

**LabGuide Studio** by **Jesse in the Lab** operates with a **strict 100% local execution invariant**:

- **No Cloud APIs**: Screenshots, screen recordings, keystrokes, Windows UI Automation trees, SQLite databases (`session.db`), audio narration tracks, and rendered `.mp4` videos are never transmitted to external servers.
- **No Telemetry or Analytics**: Zero crash-reporting beacons, usage trackers, license phone-homes, or update pings.
- **Local AI Only**:
  - Vision-language step writing and Electronic SOP block parsing communicate strictly with your local Ollama endpoint (`http://127.0.0.1:11434` / `http://localhost:11434`).
  - If Ollama is offline, LabGuide Studio falls back cleanly to deterministic Windows UIA descriptions at `0 MB` GPU VRAM.
- **Local CPU Speech & OCR**:
  - Neural voice synthesis runs locally via Kokoro-82M INT8 ONNX (`C:\Antigravity_BluePrint_Doc\models\kokoro`) or Windows SAPI (`0 MB` GPU VRAM).
  - PII detection OCR runs locally via RapidOCR ONNX Runtime (`0 MB` GPU VRAM).
- **Local-First Staging**:
  - Every new capture enters `projects/labguide_studio/sessions/_staging/` so you can delete misclicks or permanently redact/blackout regions before saving to `workflows/` or sharing exports (`guide.html`, `.docx`, `.pdf`, `.mp4`, SCORM `.zip`).
