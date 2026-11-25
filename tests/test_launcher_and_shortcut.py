from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import create_desktop_shortcut  # noqa: E402
import launcher  # noqa: E402
import session_manager  # noqa: E402
import web_server  # noqa: E402


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._raw

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class TestLauncherAndShortcut(unittest.TestCase):
    def test_generate_studio_icon_creates_valid_ico_and_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ico_path = tmp_dir / "studio.ico"
            png_path = tmp_dir / "studio.png"
            out_ico, out_png = create_desktop_shortcut.generate_studio_icon(
                ico_path=ico_path,
                png_path=png_path,
            )
            self.assertTrue(out_ico.is_file())
            self.assertTrue(out_png.is_file())
            self.assertGreater(out_ico.stat().st_size, 1024)

            with Image.open(out_png) as png_im:
                self.assertEqual(png_im.size, (512, 512))
                self.assertEqual(png_im.mode, "RGBA")

            with Image.open(out_ico) as ico_im:
                self.assertEqual(ico_im.format, "ICO")

    def test_create_windows_shortcut_creates_valid_lnk(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows .lnk shortcut test requires win32")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ico_path, _ = create_desktop_shortcut.generate_studio_icon(
                ico_path=tmp_dir / "icon.ico",
                png_path=tmp_dir / "icon.png",
            )
            lnk_path = tmp_dir / "LabGuide Studio Test.lnk"
            target_exe = Path(sys.executable)
            args_str = f'"{PROJECT_DIR / "launcher.py"}" --port 8897'
            create_desktop_shortcut.create_windows_shortcut(
                shortcut_path=lnk_path,
                target_path=target_exe,
                arguments=args_str,
                working_dir=PROJECT_DIR.parents[1],
                icon_path=ico_path,
                description="Test shortcut",
            )
            self.assertTrue(lnk_path.is_file())

            ps_read = (
                "$ws = New-Object -ComObject WScript.Shell; "
                f"$sc = $ws.CreateShortcut('{str(lnk_path).replace(chr(39), chr(39) * 2)}'); "
                "@{TargetPath=$sc.TargetPath; Arguments=$sc.Arguments; WorkingDirectory=$sc.WorkingDirectory; IconLocation=$sc.IconLocation} | ConvertTo-Json -Compress"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_read],
                check=True,
                capture_output=True,
                text=True,
            )
            data = json.loads(proc.stdout.strip())
            self.assertEqual(Path(data["TargetPath"]).resolve(), target_exe.resolve())
            self.assertEqual(data["Arguments"], args_str)
            self.assertEqual(Path(data["WorkingDirectory"]).resolve(), PROJECT_DIR.parents[1].resolve())
            self.assertTrue(data["IconLocation"].startswith(str(ico_path)))

    def test_launcher_http_probe_and_ollama_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            patchers = [
                mock.patch.object(session_manager, "SESSIONS_DIR", tmp_root / "sessions"),
                mock.patch.object(session_manager, "STAGING_DIR", tmp_root / "sessions" / "_staging"),
                mock.patch.object(session_manager, "WORKFLOWS_DIR", tmp_root / "workflows"),
            ]
            for pt in patchers:
                pt.start()
            try:
                session_manager.ensure_directories()
                server = web_server.LabGuideHTTPServer(
                    ("127.0.0.1", 0),
                    web_server.LabGuideHTTPHandler,
                    controller=web_server.ActiveRecorderController(enable_hud=False),
                )
                port = server.server_address[1]
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    self.assertTrue(launcher.is_http_alive(f"http://127.0.0.1:{port}/api/preflight"))
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3.0)

                self.assertFalse(launcher.is_http_alive(f"http://127.0.0.1:{port}/api/preflight", timeout=0.4))
            finally:
                for pt in reversed(patchers):
                    pt.stop()

        # Case 1: Ollama already online -> should not spawn subprocess
        with mock.patch("launcher.urllib.request.urlopen", return_value=_FakeHttpResponse({"models": [{"name": "gemma4:e4b"}]})), \
             mock.patch("launcher.subprocess.Popen") as mock_popen:
            res_online = launcher.ensure_ollama_running("http://127.0.0.1:11434", wait_seconds=1.0)
            self.assertTrue(res_online["online"])
            self.assertFalse(res_online["started_by_launcher"])
            self.assertTrue(res_online["has_gemma4"])
            mock_popen.assert_not_called()

        # Case 2: Ollama offline -> launches `ollama serve`, then transitions online
        fake_exe = Path(r"C:\Fake\ollama.exe")
        side_effects = [
            OSError("offline"),
            _FakeHttpResponse({"models": [{"name": "gemma4:e4b"}]}),
        ]
        with mock.patch("launcher.find_ollama_executable", return_value=fake_exe), \
             mock.patch("launcher.urllib.request.urlopen", side_effect=side_effects), \
             mock.patch("launcher.subprocess.Popen") as mock_popen:
            res_boot = launcher.ensure_ollama_running("http://127.0.0.1:11434", wait_seconds=2.0)
            self.assertTrue(res_boot["online"])
            self.assertTrue(res_boot["started_by_launcher"])
            self.assertTrue(res_boot["has_gemma4"])
            mock_popen.assert_called_once()
            self.assertEqual(mock_popen.call_args[0][0], [str(fake_exe), "serve"])


if __name__ == "__main__":
    unittest.main()
