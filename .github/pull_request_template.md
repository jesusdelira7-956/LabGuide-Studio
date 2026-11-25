## Summary
Describe the changes made to **LabGuide Studio** (`Jesse in the Lab`) and which module(s) were updated.

## Checklist
- [ ] Preserves **100% local execution** (no external cloud APIs or telemetry).
- [ ] Respects Windows 11 Per-Monitor V2 DPI awareness (`SetProcessDpiAwareness(2)`), non-blocking `pynput` hooks (`queue.Queue`), and `UIAutomationInitializerInThread()`.
- [ ] Keeps CPU pipelines (Kokoro-82M, RapidOCR, Pillow, FFmpeg) at `0 MB` GPU VRAM and passes `"keep_alive": "0"` on Ollama requests.
- [ ] Closes SQLite (`labguide_engine.connect_sqlite`), Pillow, and HTTP handles cleanly (`with` context managers).
- [ ] All tests pass with zero warnings:
  `python -W error::DeprecationWarning -W error::ResourceWarning -m unittest discover -s tests -v`
