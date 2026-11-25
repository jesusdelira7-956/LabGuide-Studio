from __future__ import annotations

import ast
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import labguide_engine  # noqa: E402
import session_manager  # noqa: E402
import web_server  # noqa: E402


class TestLabGuideWebServer(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sessions_dir = self.root / "sessions"
        self.staging_dir = self.sessions_dir / "_staging"
        self.workflows_dir = self.root / "workflows"
        self.sops_dir = self.root / "directives" / "sops"
        self.guides_dir = self.root / "knowledge" / "guides"

        self.patchers = [
            patch.object(session_manager, "SESSIONS_DIR", self.sessions_dir),
            patch.object(session_manager, "STAGING_DIR", self.staging_dir),
            patch.object(session_manager, "WORKFLOWS_DIR", self.workflows_dir),
            patch.object(
                session_manager,
                "SAVE_PRESETS",
                {
                    "workflows": self.workflows_dir,
                    "directives_sops": self.sops_dir,
                    "knowledge_guides": self.guides_dir,
                },
            ),
        ]
        for pt in self.patchers:
            pt.start()

        session_manager.ensure_directories()
        self.server = web_server.LabGuideHTTPServer(("127.0.0.1", 0), web_server.LabGuideHTTPHandler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3.0)
        for pt in reversed(self.patchers):
            pt.stop()
        self.tmp.cleanup()

    def _get(self, path: str) -> tuple[int, bytes, str]:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=5) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")

    def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _create_staging_session(self, name: str = "20261124_100000") -> Path:
        sdir = self.staging_dir / name
        engine = labguide_engine.LabGuideEngine(
            session_dir=sdir,
            title="Test Staging Workflow",
            redact_pii=False,
            auto_compile=False,
            session_status="staging",
        )
        for step_num in (1, 2):
            img = labguide_engine.Image.new("RGB", (300, 200), color=(30, 60, 90))
            # Sharp pixel grid at center for blur verification
            for x in range(40, 120):
                for y in range(40, 90):
                    img.putpixel((x, y), (255, 255, 255) if (x + y) % 2 == 0 else (0, 0, 0))
            img.save(sdir / f"assets/raw/step_{step_num:03d}_raw.png", "PNG")
            img.save(sdir / f"assets/step_{step_num:03d}_full.png", "PNG")
            img.save(sdir / f"assets/step_{step_num:03d}_crop.png", "PNG")

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
                    100.0,
                    "click",
                    80,
                    65,
                    "Settings",
                    "Network",
                    "Button",
                    json.dumps({"monitor": {"left": 0, "top": 0}}),
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
                    101.0,
                    "click",
                    150,
                    100,
                    "Settings",
                    "Save",
                    "Button",
                    json.dumps({"monitor": {"left": 0, "top": 0}}),
                    "assets/step_002_full.png",
                    "assets/step_002_crop.png",
                ),
            )
        engine.compile_guide(use_ollama=False)
        return sdir

    def test_ui_shell_and_preflight_served(self) -> None:
        status, body, ctype = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        html_text = body.decode("utf-8")
        self.assertIn("<!DOCTYPE html>", html_text)
        self.assertIn("LabGuide Studio", html_text)
        self.assertIn("Jesse in the Lab", html_text)
        self.assertNotIn("S" "cribe", html_text)
        self.assertIn("Help &amp; Pro Guide", html_text)
        self.assertIn('<dialog id="confirm-dialog"', html_text)
        self.assertIn('<dialog id="about-dialog"', html_text)

        st_logo, logo_bytes, logo_ctype = self._get("/brand/logo.png")
        self.assertEqual(st_logo, 200)
        self.assertIn("image/png", logo_ctype)
        self.assertGreater(len(logo_bytes), 1024)

        st_bg, bg_bytes, bg_ctype = self._get("/brand/bg.jpg")
        self.assertEqual(st_bg, 200)
        self.assertIn("image/jpeg", bg_ctype)
        self.assertGreater(len(bg_bytes), 1024)

        st_ico, ico_bytes, _ = self._get("/favicon.ico")
        self.assertEqual(st_ico, 200)
        self.assertGreater(len(ico_bytes), 512)

        st_about, about_raw, _ = self._get("/api/about")
        self.assertEqual(st_about, 200)
        about = json.loads(about_raw.decode("utf-8"))
        self.assertEqual(about["app_name"], "LabGuide Studio")
        self.assertEqual(about["publisher"], "Jesse in the Lab")
        self.assertEqual(about["license"], "MIT License")

        st_code, preflight_raw, _ = self._get("/api/preflight")
        self.assertEqual(st_code, 200)
        preflight = json.loads(preflight_raw.decode("utf-8"))
        self.assertIn("python", preflight)
        self.assertIn("dependencies", preflight)
        self.assertIn("folders", preflight)

    def test_staging_review_edit_blur_save_and_discard_flow(self) -> None:
        sdir = self._create_staging_session()

        # 1. Appears in staging list
        _, raw_list, _ = self._get("/api/sessions")
        sessions = json.loads(raw_list.decode("utf-8"))
        self.assertEqual(len(sessions["staging"]), 1)
        self.assertEqual(sessions["staging"][0]["title"], "Test Staging Workflow")

        # 2. Edit step 1 text
        _, detail = self._post_json(
            "/api/sessions/step/update",
            {
                "path": str(sdir),
                "step_num": 1,
                "step_title": "Open Network Settings",
                "step_instruction": "Click Network in Windows Settings.",
                "step_note": "Verify Wi-Fi is connected.",
            },
        )
        self.assertEqual(detail["steps"][0]["step_title"], "Open Network Settings")
        self.assertIn("Open Network Settings", detail["guide_md"])

        # 3. Move step 2 up -> becomes step 1
        _, moved = self._post_json(
            "/api/sessions/step/move",
            {"path": str(sdir), "step_num": 2, "direction": "up"},
        )
        self.assertEqual(moved["steps"][0]["control_name"], "Save")
        self.assertEqual(moved["steps"][1]["control_name"], "Network")

        # 4. Apply manual blur to step 1 crop and verify pixel change + reset from raw
        crop_path = sdir / moved["steps"][0]["crop_image"]
        with labguide_engine.Image.open(crop_path) as before_img:
            before_px = before_img.getpixel((80, 65))

        _, blurred = self._post_json(
            "/api/sessions/step/blur",
            {
                "path": str(sdir),
                "step_num": 1,
                "target": "crop",
                "boxes": [{"x": 0.15, "y": 0.2, "w": 0.25, "h": 0.25}],
            },
        )
        with labguide_engine.Image.open(crop_path) as after_img:
            after_px = after_img.getpixel((80, 65))
        self.assertNotEqual(before_px, after_px)
        self.assertTrue(blurred["has_html"])

        _, reset_detail = self._post_json(
            "/api/sessions/step/reset-image",
            {"path": str(sdir), "step_num": 1, "redact_pii": False},
        )
        self.assertEqual(len(reset_detail["steps"]), 2)

        # 5. Delete step 2 -> 1 step remains
        _, after_del = self._post_json(
            "/api/sessions/step/delete",
            {"path": str(sdir), "step_num": 2},
        )
        self.assertEqual(len(after_del["steps"]), 1)

        # 6. Save to workflows library (promotes out of staging)
        _, saved_res = self._post_json(
            "/api/sessions/save",
            {"path": str(sdir), "preset": "workflows", "custom_name": "Firewall Demo"},
        )
        saved_path = Path(saved_res["saved_path"])
        self.assertTrue(saved_path.is_relative_to(self.workflows_dir))
        self.assertTrue((saved_path / "guide.md").is_file())
        self.assertTrue((saved_path / "guide.html").is_file())
        self.assertFalse(sdir.exists())

        # 7. Discard saved workflow
        _, disc_res = self._post_json("/api/sessions/discard", {"path": str(saved_path)})
        self.assertTrue(disc_res["discarded"])
        self.assertFalse(saved_path.exists())

    def test_path_jail_rejects_outside_workspace_paths(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(f"/api/sessions/detail?path={urllib.parse.quote('C:/Windows/System32')}")
        self.assertEqual(ctx.exception.code, 400)

    def test_blackout_and_multi_format_exports(self) -> None:
        sdir = self._create_staging_session("20261124_103000")
        qpath = urllib.parse.quote(str(sdir))

        # 1. Solid Blackout mode
        _, _ = self._post_json(
            "/api/sessions/step/blur",
            {
                "path": str(sdir),
                "step_num": 1,
                "target": "crop",
                "mode": "blackout",
                "boxes": [{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}],
            },
        )
        with labguide_engine.Image.open(sdir / "assets/step_001_crop.png") as im:
            self.assertEqual(im.getpixel((45, 30)), (9, 13, 22))

        # 2. Markdown + Base64 Markdown
        st_md, md_bytes, _ = self._get(f"/api/sessions/export?path={qpath}&format=md")
        self.assertEqual(st_md, 200)
        self.assertIn("./assets/step_001_crop.png", md_bytes.decode("utf-8"))

        st_b64, b64_bytes, _ = self._get(f"/api/sessions/export?path={qpath}&format=md_b64&inline=1")
        self.assertEqual(st_b64, 200)
        self.assertIn("data:image/png;base64,", b64_bytes.decode("utf-8"))

        # 3. HTML + Print PDF
        st_html, html_bytes, _ = self._get(f"/api/sessions/export?path={qpath}&format=html")
        self.assertEqual(st_html, 200)
        self.assertIn("<!DOCTYPE html>", html_bytes.decode("utf-8"))

        st_pdf, pdf_bytes, _ = self._get(f"/api/sessions/export?path={qpath}&format=pdf_print")
        self.assertEqual(st_pdf, 200)
        self.assertIn("window.print()", pdf_bytes.decode("utf-8"))

        # 4. DOCX ZIP structure
        st_docx, docx_bytes, ctype_docx = self._get(f"/api/sessions/export?path={qpath}&format=docx")
        self.assertEqual(st_docx, 200)
        self.assertIn("wordprocessingml", ctype_docx)
        with zipfile.ZipFile(io.BytesIO(docx_bytes), "r") as zf:
            names = set(zf.namelist())
            self.assertIn("word/document.xml", names)
            self.assertIn("word/media/step_001.png", names)

        # 5. JSON + Replay Script (valid Python AST)
        st_json, json_bytes, _ = self._get(f"/api/sessions/export?path={qpath}&format=json")
        self.assertEqual(st_json, 200)
        parsed_json = json.loads(json_bytes.decode("utf-8"))
        self.assertEqual(parsed_json["schema"], "labguide-studio/v1")
        self.assertEqual(len(parsed_json["steps"]), 2)

        st_rep, rep_bytes, _ = self._get(f"/api/sessions/export?path={qpath}&format=replay_py")
        self.assertEqual(st_rep, 200)
        ast.parse(rep_bytes.decode("utf-8"))

    def test_official_guide_and_media_range_streaming(self) -> None:
        st_info, raw_info, _ = self._get("/api/official-guide")
        self.assertEqual(st_info, 200)
        info = json.loads(raw_info.decode("utf-8"))
        self.assertIn("available", info)
        self.assertIn("html_url", info)

        # Create session with step audio + walkthrough.mp4 and verify compile + Range serving
        sdir = self._create_staging_session("20261124_104500")
        audio_dir = sdir / "assets" / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / "step_001.mp3").write_bytes(b"ID3FAKEAUDIO" * 30)
        (sdir / "walkthrough.mp4").write_bytes(b"FAKEVIDEOSTREAMBYTES" * 40)

        engine = labguide_engine.LabGuideEngine(
            session_dir=sdir,
            title=None,
            redact_pii=False,
            session_status=None,
        )
        engine.compile_guide(use_ollama=False)

        html_text = (sdir / "guide.html").read_text(encoding="utf-8")
        self.assertIn('<audio class="step-audio"', html_text)
        self.assertIn("Play Narrated Visual Tour", html_text)
        self.assertIn("walkthrough.mp4", html_text)

        # Served via /api/sessions/html rewrites ./walkthrough.mp4 to API endpoint
        _, served_html, _ = self._get(
            f"/api/sessions/html?path={urllib.parse.quote(str(sdir))}"
        )
        self.assertIn("/api/sessions/asset?session=", served_html.decode("utf-8"))

        # Range request on walkthrough.mp4 returns 206 Partial Content
        req = urllib.request.Request(
            f"{self.base_url}/api/sessions/asset?session={urllib.parse.quote(str(sdir))}&file=walkthrough.mp4",
            headers={"Range": "bytes=0-19"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 206)
            self.assertEqual(len(resp.read()), 20)
            self.assertTrue(resp.headers.get("Content-Range", "").startswith("bytes 0-19/"))

        # Non-allowlisted file inside session root must still return 403
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(
                f"/api/sessions/asset?session={urllib.parse.quote(str(sdir))}&file=session.db"
            )
        self.assertEqual(ctx.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
