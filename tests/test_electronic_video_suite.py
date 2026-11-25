from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import exporter  # noqa: E402
import presenter_engine  # noqa: E402
import labguide_engine  # noqa: E402
import session_manager  # noqa: E402
import sop_importer  # noqa: E402
import tts_engine  # noqa: E402
import web_server  # noqa: E402


class TestElectronicVideoSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sessions_dir = self.root / "sessions"
        self.staging_dir = self.sessions_dir / "_staging"
        self.workflows_dir = self.root / "workflows"
        self.avatars_dir = self.root / "avatars"

        self.patchers = [
            patch.object(session_manager, "SESSIONS_DIR", self.sessions_dir),
            patch.object(session_manager, "STAGING_DIR", self.staging_dir),
            patch.object(session_manager, "WORKFLOWS_DIR", self.workflows_dir),
            patch.object(
                session_manager,
                "SAVE_PRESETS",
                {"workflows": self.workflows_dir},
            ),
            patch.object(presenter_engine, "AVATARS_DIR", self.avatars_dir),
            patch.object(presenter_engine, "MANIFEST_PATH", self.avatars_dir / "manifest.json"),
        ]
        for pt in self.patchers:
            pt.start()

        session_manager.ensure_directories()
        self.server = web_server.LabGuideHTTPServer(
            ("127.0.0.1", 0),
            web_server.LabGuideHTTPHandler,
            controller=web_server.ActiveRecorderController(enable_hud=False),
        )
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3.0)
        for pt in reversed(self.patchers):
            pt.stop()
        self.tmp.cleanup()

    def test_electronic_sop_importer_slides_iso_quiz_and_scorm(self) -> None:
        md_sop = (
            "# Deploy Local Firewall Policy\n\n"
            "## 1. Open Defender Firewall\n"
            "Launch Windows Defender Firewall with Advanced Security. [pause 0.5s]\n\n"
            "## 2. Create Inbound Rule\n"
            "Click Inbound Rules and add port 8897 for localhost testing.\n"
        )
        detail = sop_importer.import_electronic_sop(
            raw_text=md_sop,
            title="Deploy Local Firewall Policy",
            use_ollama=False,
            doc_id="SOP-SEC-01",
            owner="SecOps Team",
        )
        sdir = Path(detail["path"])
        self.assertEqual(len(detail["steps"]), 2)
        self.assertEqual(detail["metadata"]["doc_id"], "SOP-SEC-01")
        with Image.open(sdir / detail["steps"][0]["full_image"]) as im:
            self.assertEqual(im.size, (1920, 1080))

        # Update ISO header & generate 3-question offline quiz
        iso_detail = session_manager.update_iso_metadata(
            sdir,
            doc_id="SOP-SEC-02",
            version="2.0",
            owner="Platform Ops",
            effective_date="2026-11-24",
            prerequisites="Administrator PowerShell",
            safety_warnings="Do not open external WAN ports.",
        )
        self.assertEqual(iso_detail["metadata"]["doc_id"], "SOP-SEC-02")

        quiz_detail = session_manager.generate_session_quiz(sdir, use_ollama=False)
        quiz = json.loads(quiz_detail["metadata"]["quiz_json"])
        self.assertEqual(len(quiz), 3)

        html_text = (sdir / "guide.html").read_text(encoding="utf-8")
        self.assertIn("SOP-SEC-02", html_text)
        self.assertIn("Knowledge Check", html_text)
        self.assertIn('type="radio"', html_text)

        # Replace step 1 image cleanly
        buf = io.BytesIO()
        Image.new("RGB", (640, 360), color=(12, 88, 120)).save(buf, "PNG")
        replaced = session_manager.replace_step_image(sdir, 1, buf.getvalue())
        with Image.open(sdir / replaced["steps"][0]["full_image"]) as rep_im:
            self.assertEqual(rep_im.getpixel((10, 10)), (12, 88, 120))

        # SCORM 1.2 ZIP and Offline Bundle ZIP exports via HTTP
        qpath = urllib.parse.quote(str(sdir))
        with urllib.request.urlopen(f"{self.base_url}/api/sessions/export?path={qpath}&format=scorm", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            scorm_bytes = resp.read()
        with zipfile.ZipFile(io.BytesIO(scorm_bytes), "r") as zf:
            names = set(zf.namelist())
            self.assertIn("imsmanifest.xml", names)
            self.assertIn("scorm_wrapper.js", names)
            self.assertIn("index.html", names)

        with urllib.request.urlopen(f"{self.base_url}/api/sessions/export?path={qpath}&format=offline_zip", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            bundle_bytes = resp.read()
        with zipfile.ZipFile(io.BytesIO(bundle_bytes), "r") as zf:
            names = set(zf.namelist())
            self.assertIn("guide.html", names)
            self.assertIn("guide.docx", names)
            self.assertIn("walkthrough.srt", names)

    def test_docx_extraction_and_pause_srt_parsing(self) -> None:
        # Build minimal docx bytes in memory
        doc_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            "<w:p><w:r><w:t>1. Open Studio</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>2. Export SCORM package</w:t></w:r></w:p>"
            "</w:body></w:document>"
        )
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w") as zf:
            zf.writestr("word/document.xml", doc_xml)
        extracted = sop_importer.extract_text_from_upload("runbook.docx", zbuf.getvalue())
        self.assertIn("Open Studio", extracted)
        self.assertIn("Export SCORM package", extracted)

        segs = tts_engine.parse_pause_segments("Click Save [pause 1.5s] then verify.")
        self.assertEqual(segs, [("text", "Click Save"), ("pause", 1.5), ("text", "then verify.")])

        srt = tts_engine.build_srt_subtitles(
            [{"step_num": 1, "text": "Click Save [pause 1s] then verify.", "start_sec": 0.0, "duration_sec": 4.0}]
        )
        self.assertIn("00:00:00,000 --> 00:00:04,000", srt)
        self.assertNotIn("[pause", srt)

    def test_presenter_consent_guardrail_and_manifest(self) -> None:
        avatars = presenter_engine.list_avatars(self.avatars_dir)
        self.assertEqual(len(avatars), 3)
        self.assertTrue((self.avatars_dir / "manifest.json").is_file())

        png_buf = io.BytesIO()
        Image.new("RGB", (200, 200), color=(40, 90, 140)).save(png_buf, "PNG")

        with self.assertRaises(ValueError):
            presenter_engine.register_custom_avatar(
                name="Unauthorized",
                role_title="Presenter",
                image_bytes=png_buf.getvalue(),
                consent_confirmed=False,
                avatars_dir=self.avatars_dir,
            )

        entry = presenter_engine.register_custom_avatar(
            name="Studio Owner",
            role_title="Lead Trainer",
            image_bytes=png_buf.getvalue(),
            consent_confirmed=True,
            avatars_dir=self.avatars_dir,
        )
        self.assertTrue(entry["consent_confirmed"])
        self.assertTrue((self.avatars_dir / entry["file"]).is_file())


if __name__ == "__main__":
    unittest.main()
