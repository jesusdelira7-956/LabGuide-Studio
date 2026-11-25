# Third-Party Open-Source Notices & Attributions

**LabGuide Studio** (`© 2026 Jesse in the Lab`, licensed under the [MIT License](LICENSE)) integrates or invokes the following open-source libraries, local models, and system tools on Windows 11. Each component remains subject to its respective upstream license:

| Component | License | Role in LabGuide Studio |
| --- | --- | --- |
| **Kokoro-82M ONNX (`kokoro-onnx`)** | Apache-2.0 | 100% local CPU neural voiceover synthesis |
| **Ollama** | MIT | Local REST inference server (`127.0.0.1:11434`) for VLM step writing & SOP parsing |
| **FFmpeg** | LGPL / GPL (External CLI binary) | Smooth `zoompan`, `.srt` caption overlay, and PiP `.mp4` compositing |
| **RapidOCR ONNX Runtime** | Apache-2.0 | CPU-only optical character recognition for PII auto-redaction |
| **Pillow (`PIL`)** | HPND | Screenshot annotation, callout overlays, privacy blurs, and 1080p slide rendering |
| **pypdf** | BSD-3-Clause | Electronic `.pdf` SOP text extraction |
| **mss** | MIT | DPI-aware multi-monitor screen frame capture |
| **pynput** | LGPL-3.0 | Non-blocking Windows mouse and keyboard event capture |
| **uiautomation** | Apache-2.0 | Windows UI Automation accessibility metadata inspection |
| **requests** | Apache-2.0 | Localhost Ollama API communication |
| **soundfile** | BSD-3-Clause | 24 kHz WAV synthesis output |
| **Built-In Studio Presenters** (`aria_architect`, `marcus_sre`, `nova_ai`) | CC0-1.0 / Original Synthetic Artwork | Consent-safe synthetic PiP presenter portraits generated locally via Pillow |

## Custom Avatar Provenance Policy

Custom presenter uploads are stored locally in `projects/labguide_studio/assets/avatars/manifest.json` and require explicit user ownership/consent confirmation (`consent_confirmed=true`) before registration or PiP rendering.
