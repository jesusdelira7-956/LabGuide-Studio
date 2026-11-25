from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import labguide_engine  # noqa: E402

# Minimal valid 1x1 PNG for fallback when Pillow is not installed in the active interpreter
TINY_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aW1sAAAAASUVORK5CYII="
)


class FakeShot:
    def __init__(self, width: int, height: int) -> None:
        self.size = (width, height)
        self.bgra = b"\x20\x40\x60\xff" * (width * height)


class FakeMSS:
    def __init__(self, width: int = 800, height: int = 600) -> None:
        self.monitors = [
            {"left": 0, "top": 0, "width": width, "height": height},
            {"left": 0, "top": 0, "width": width, "height": height},
        ]

    def grab(self, monitor: dict) -> FakeShot:
        return FakeShot(monitor["width"], monitor["height"])


class TestLabGuideEngine(unittest.TestCase):
    def test_db_initialization_and_recompile_preserves_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "session_01"
            engine = labguide_engine.LabGuideEngine(
                session_dir=session_dir,
                title="Firewall Setup",
                redact_pii=False,
            )
            with labguide_engine.connect_sqlite(engine.db_path) as conn:
                meta = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
            self.assertEqual(meta["title"], "Firewall Setup")
            original_created_at = meta["created_at"]

            recompiled = labguide_engine.LabGuideEngine(
                session_dir=session_dir,
                title=None,
                redact_pii=False,
            )
            with labguide_engine.connect_sqlite(recompiled.db_path) as conn:
                meta_after = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
            self.assertEqual(meta_after["title"], "Firewall Setup")
            self.assertEqual(meta_after["created_at"], original_created_at)

    def test_extract_key_char_windows_ctrl_codes(self) -> None:
        self.assertEqual(labguide_engine.extract_key_char(SimpleNamespace(vk=80, char="\x10")), "p")
        self.assertEqual(labguide_engine.extract_key_char(SimpleNamespace(vk=83, char="\x13")), "s")
        self.assertEqual(labguide_engine.extract_key_char(SimpleNamespace(char="\x10")), "p")
        self.assertEqual(labguide_engine.extract_key_char(SimpleNamespace(char="\x13")), "s")
        self.assertEqual(labguide_engine.extract_key_char(SimpleNamespace(char="C")), "c")
        self.assertEqual(labguide_engine.extract_key_char(SimpleNamespace(vk=None, char=None)), "")

    @unittest.skipIf(
        labguide_engine.Image is None or labguide_engine.np is None,
        "Pillow and numpy are required for PII blur test",
    )
    def test_pii_redaction_blurs_matching_regions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = labguide_engine.LabGuideEngine(
                session_dir=Path(tmp),
                title="PII Test",
                redact_pii=False,
            )
            img = labguide_engine.Image.new("RGB", (200, 100), color=(255, 255, 255))
            # Draw sharp contrasting stripes inside the email region (10..90, 10..40)
            for x in range(10, 90):
                for y in range(10, 40):
                    img.putpixel((x, y), (0, 0, 0) if x % 2 == 0 else (255, 255, 255))
            # Draw sharp contrasting pixel inside safe region (120..180, 60..90)
            img.putpixel((150, 75), (255, 0, 0))

            engine.ocr_engine = lambda _arr: (
                [
                    ([[10, 10], [90, 10], [90, 40], [10, 40]], "admin@example.com", 0.99),
                    ([[120, 60], [180, 60], [180, 90], [120, 90]], "Save Button", 0.99),
                ],
                None,
            )

            redacted = engine._apply_pii_blur(img)
            self.assertNotEqual(redacted.getpixel((50, 25)), img.getpixel((50, 25)))
            self.assertEqual(redacted.getpixel((150, 75)), (255, 0, 0))

    @unittest.skipIf(labguide_engine.Image is None, "Pillow is required for capture annotation test")
    def test_capture_and_annotate_writes_raw_full_and_crop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "cap"
            engine = labguide_engine.LabGuideEngine(
                session_dir=session_dir,
                title="Capture Test",
                redact_pii=False,
            )
            full_rel, crop_rel = engine._capture_and_annotate(
                FakeMSS(),
                step_num=1,
                x=300,
                y=250,
                rect=[260, 230, 340, 270],
            )
            self.assertEqual(full_rel, "assets/step_001_full.png")
            self.assertEqual(crop_rel, "assets/step_001_crop.png")
            self.assertTrue((session_dir / "assets" / "raw" / "step_001_raw.png").is_file())
            self.assertTrue((session_dir / full_rel).is_file())
            self.assertTrue((session_dir / crop_rel).is_file())

    def test_compile_guide_offline_fallback_and_valid_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "offline_session"
            engine = labguide_engine.LabGuideEngine(
                session_dir=session_dir,
                title="Windows <Firewall> Guide",
                redact_pii=False,
            )
            for idx in (1, 2):
                (session_dir / f"assets/step_{idx:03d}_full.png").write_bytes(TINY_PNG_BYTES)
                (session_dir / f"assets/step_{idx:03d}_crop.png").write_bytes(TINY_PNG_BYTES)

            with labguide_engine.connect_sqlite(engine.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO steps (
                        step_num, timestamp, event_type, x, y,
                        window_title, control_name, control_type, uia_json,
                        full_image, crop_image
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        1000.0,
                        "click",
                        120,
                        240,
                        "Windows Defender Firewall",
                        "Inbound Rules",
                        "TreeItem",
                        "{}",
                        "assets/step_001_full.png",
                        "assets/step_001_crop.png",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO steps (
                        step_num, timestamp, event_type, x, y,
                        window_title, control_name, control_type, uia_json,
                        full_image, crop_image
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        2,
                        1002.0,
                        "shortcut",
                        0,
                        0,
                        "Active Application",
                        "Ctrl+S",
                        "Shortcut",
                        "{}",
                        "assets/step_002_full.png",
                        "assets/step_002_crop.png",
                    ),
                )

            with (
                patch.object(engine, "_post_ollama_generate", side_effect=OSError("offline")),
                patch("labguide_engine.time.sleep", return_value=None),
            ):
                engine.compile_guide()

            md_text = (session_dir / "guide.md").read_text(encoding="utf-8")
            html_text = (session_dir / "guide.html").read_text(encoding="utf-8")

            self.assertIn("# Windows <Firewall> Guide", md_text)
            self.assertIn("## Step 1: Click Inbound Rules", md_text)
            self.assertIn("![Step 1: Click Inbound Rules](./assets/step_001_crop.png)", md_text)
            self.assertIn("<details>", md_text)
            self.assertIn("./assets/step_001_full.png", md_text)
            self.assertIn("## Step 2: Press Ctrl+S", md_text)
            self.assertIn("In Active Application, press Ctrl+S.", md_text)

            self.assertIn("<!DOCTYPE html>", html_text)
            self.assertIn("Windows &lt;Firewall&gt; Guide", html_text)
            self.assertIn('<section class="step"', html_text)
            self.assertIn("data:image/png;base64,", html_text)

    def test_compile_guide_with_mocked_ollama_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "vlm_session"
            engine = labguide_engine.LabGuideEngine(
                session_dir=session_dir,
                title="VLM Guide",
                redact_pii=False,
            )
            (session_dir / "assets/step_001_full.png").write_bytes(TINY_PNG_BYTES)
            (session_dir / "assets/step_001_crop.png").write_bytes(TINY_PNG_BYTES)

            with labguide_engine.connect_sqlite(engine.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO steps (
                        step_num, timestamp, event_type, x, y,
                        window_title, control_name, control_type, uia_json,
                        full_image, crop_image
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        1000.0,
                        "click",
                        50,
                        60,
                        "Settings",
                        "Save",
                        "Button",
                        "{}",
                        "assets/step_001_full.png",
                        "assets/step_001_crop.png",
                    ),
                )

            mock_response = {
                "response": json.dumps(
                    {
                        "title": "Open Firewall Rules",
                        "instruction": "Click Inbound Rules in the left pane.",
                        "note": "Requires elevation.",
                    }
                )
            }
            with patch.object(engine, "_post_ollama_generate", return_value=mock_response):
                engine.compile_guide()

            md_text = (session_dir / "guide.md").read_text(encoding="utf-8")
            html_text = (session_dir / "guide.html").read_text(encoding="utf-8")
            self.assertIn("## Step 1: Open Firewall Rules", md_text)
            self.assertIn("Click Inbound Rules in the left pane.", md_text)
            self.assertIn("> **Note:** Requires elevation.", md_text)
            self.assertIn("Requires elevation.", html_text)

            with labguide_engine.connect_sqlite(engine.db_path) as conn:
                row = conn.execute(
                    "SELECT step_title, step_instruction, step_note FROM steps WHERE step_num = 1"
                ).fetchone()
            self.assertEqual(
                row,
                ("Open Firewall Rules", "Click Inbound Rules in the left pane.", "Requires elevation."),
            )

    @unittest.skipIf(labguide_engine.mss is None, "mss is required for MSS factory test")
    def test_worker_loop_uses_non_deprecated_mss_factory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = labguide_engine.LabGuideEngine(
                session_dir=Path(tmp) / "mss_session",
                title="MSS Check",
                redact_pii=False,
            )
            engine.event_queue.put(None)
            with patch.object(labguide_engine, "auto", None):
                engine._worker_loop()

    def test_self_click_exclusion_and_smart_auto_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = labguide_engine.LabGuideEngine(
                session_dir=Path(tmp) / "auto_title",
                title="Standard Operating Procedure",
                redact_pii=False,
            )
            self.assertTrue(
                engine._is_excluded_window("LabGuide Studio | Jesse in the Lab")
            )
            self.assertTrue(engine._is_excluded_window("LabGuide Studio Mini-HUD"))
            self.assertFalse(engine._is_excluded_window("Windows PowerShell"))

            engine._apply_smart_auto_title("Inbound Rules - Windows Defender Firewall")
            self.assertTrue(engine.title.startswith("SOP - Windows Defender Firewall ("))
            with labguide_engine.connect_sqlite(engine.db_path) as conn:
                stored = conn.execute(
                    "SELECT value FROM metadata WHERE key = 'title'"
                ).fetchone()[0]
            self.assertEqual(stored, engine.title)

            custom = labguide_engine.LabGuideEngine(
                session_dir=Path(tmp) / "custom_title",
                title="My Custom SOP",
                redact_pii=False,
            )
            custom._apply_smart_auto_title("Windows Settings")
            self.assertEqual(custom.title, "My Custom SOP")

    @unittest.skipIf(labguide_engine.Image is None, "Pillow is required for typing coalescer test")
    def test_typing_coalescer_and_password_masking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sdir = Path(tmp) / "typing_session"
            engine = labguide_engine.LabGuideEngine(
                session_dir=sdir,
                title="Login Demo",
                redact_pii=False,
                auto_compile=False,
            )

            # Enqueue: "hello" (with backspace), click, then password "SuperSecret123!"
            for ch in ("h", "e", "l", "l", "x", "<BACKSPACE>", "o"):
                engine.event_queue.put(labguide_engine.RawEvent("key", 100.0, shortcut=ch))
            engine.event_queue.put(labguide_engine.RawEvent("click", 101.0, x=200, y=150))
            for ch in "SuperSecret123!":
                engine.event_queue.put(labguide_engine.RawEvent("key", 102.0, shortcut=ch))
            engine.event_queue.put(None)

            focused_responses = [
                {
                    "window_title": "App Login",
                    "control_name": "Username",
                    "control_type": "Edit",
                    "automation_id": "txtUser",
                    "rect": [100, 80, 260, 110],
                    "is_password": False,
                },
                {
                    "window_title": "App Login",
                    "control_name": "Password",
                    "control_type": "Edit",
                    "automation_id": "txtPass",
                    "rect": [100, 120, 260, 150],
                    "is_password": True,
                },
            ]

            class FakeCtxMgr:
                def __enter__(self) -> FakeMSS:
                    return FakeMSS()

                def __exit__(self, *args: object) -> None:
                    return None

            fake_mss = SimpleNamespace(MSS=lambda: FakeCtxMgr())
            with (
                patch.object(labguide_engine, "mss", fake_mss),
                patch.object(labguide_engine, "auto", None),
                patch.object(engine, "_query_focused_uia", side_effect=focused_responses),
                patch.object(
                    engine,
                    "_query_uia_at_point",
                    return_value={
                        "window_title": "App Login",
                        "control_name": "Next",
                        "control_type": "Button",
                        "automation_id": "btnNext",
                        "rect": [150, 130, 250, 170],
                    },
                ),
            ):
                engine._worker_loop()

            engine.compile_guide(use_ollama=False)
            with labguide_engine.connect_sqlite(engine.db_path) as conn:
                rows = conn.execute(
                    "SELECT step_num, event_type, control_name, uia_json FROM steps ORDER BY step_num ASC"
                ).fetchall()

            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0][1], "type")
            self.assertEqual(json.loads(rows[0][3])["typed_text"], "hello")
            self.assertEqual(rows[1][1], "click")
            self.assertEqual(rows[2][1], "type")
            self.assertEqual(json.loads(rows[2][3])["typed_text"], "••••••••")
            self.assertTrue(json.loads(rows[2][3])["is_sensitive"])

            md_text = (sdir / "guide.md").read_text(encoding="utf-8")
            html_text = (sdir / "guide.html").read_text(encoding="utf-8")
            self.assertIn("hello", md_text)
            self.assertNotIn("SuperSecret123!", md_text)
            self.assertNotIn("SuperSecret123!", html_text)
            self.assertIn("masked for privacy", md_text)

    def test_ollama_payload_includes_keep_alive_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sdir = Path(tmp) / "keepalive"
            engine = labguide_engine.LabGuideEngine(
                session_dir=sdir,
                title="VRAM Check",
                redact_pii=False,
            )
            (sdir / "assets/step_001_crop.png").write_bytes(TINY_PNG_BYTES)
            captured_payload: dict = {}

            def fake_post(payload: dict) -> dict:
                captured_payload.update(payload)
                return {
                    "response": json.dumps(
                        {"title": "Click Save", "instruction": "Click Save.", "note": ""}
                    )
                }

            with patch.object(engine, "_post_ollama_generate", side_effect=fake_post):
                engine._describe_step_with_ollama(
                    {
                        "window_title": "Settings",
                        "control_name": "Save",
                        "control_type": "Button",
                        "event_type": "click",
                        "crop_image": "assets/step_001_crop.png",
                    }
                )
            self.assertEqual(captured_payload.get("keep_alive"), "0")


if __name__ == "__main__":
    unittest.main()
