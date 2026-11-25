# Security Policy & Threat Model

**LabGuide Studio** (`© 2026 Jesse in the Lab`) is architected as a **localhost-only, 100% local workstation studio** on Windows 11.

## Security Controls Enforced in Code

1. **Loopback-Only Binding & Local Origin Verification**
   - `web_server.py` binds exclusively to `127.0.0.1` (default port `8897`).
   - Mutating `POST` routes verify `_is_local_origin()` (`127.0.0.1` / `localhost`) before processing JSON payloads.

2. **Strict Path Jail (`session_manager.resolve_safe_session_dir`)**
   - Every session read, edit, blur, render, delete, promote, or export resolves paths via `Path.resolve()` and rejects any path outside allowlisted LabGuide Studio directories (`sessions/_staging/`, `workflows/`, `sessions/`, `directives/sops/`, `knowledge/guides/`).
   - Folder-open requests validate targets via `resolve_safe_open_target()` before invoking Windows Explorer.

3. **Automatic Sensitive Data & Password Protection**
   - **Password Field Masking**: Keystroke coalescing in `labguide_engine.py` inspects Windows UIA `IsPassword` and sensitive field-name heuristics (`password`, `passcode`, `token`, `secret`, `api key`, `pin`) and masks captured text as `••••••••`.
   - **CPU OCR PII Redaction**: `RapidOCR` scans captured screenshots locally on CPU and blurs matching keywords (tokens, keys, emails, passwords) before AI processing, while retaining unedited `assets/raw/` frames locally for reproducibility.
   - **Manual Blur & Blackout Tools**: Users can review every draft in `sessions/_staging/` and apply irreversible pixel blur or solid blackout regions before saving or compiling.

4. **Avatar Consent & Provenance Ledger**
   - Built-in Studio avatars are 100% synthetic original artwork (`CC0-1.0`).
   - Custom avatar uploads and Picture-in-Picture rendering enforce explicit `consent_confirmed=True` validation and record entries in `assets/avatars/manifest.json`.

5. **SQLite & Desktop Hook Safety**
   - Low-level Windows hooks push raw events onto an in-memory `queue.Queue` (zero blocking I/O or UIA calls inside `pynput` callbacks).
   - Background UIA threads enter COM via `uiautomation.UIAutomationInitializerInThread()`.
   - All SQLite `.db` accesses use deterministic context-managed connections (`labguide_engine.connect_sqlite`).

## Reporting Vulnerabilities

If you discover a path-traversal, XSS/HTML-injection issue in exported guides, or privilege issue in a fork or release, document the minimal reproduction steps locally and verify fixes against `tests/test_web_server.py` (`test_path_jail_rejects_outside_workspace_paths`) and `execution/security/asset_verifier.py`.
