from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import brand  # noqa: E402

WORKSPACE_ROOT = brand.get_runtime_root()

ASSETS_DIR = brand.ASSETS_DIR
DEFAULT_ICO_PATH = brand.ICO_PATH
DEFAULT_PNG_PATH = brand.LOGO_PNG_PATH
DEFAULT_SHORTCUT_NAME = f"{brand.APP_NAME}.lnk"

ICO_SIZES: list[tuple[int, int]] = brand.ICO_SIZES


def _ps_quote(value: str) -> str:
    return value.replace("'", "''")


def generate_studio_icon(
    ico_path: Path = DEFAULT_ICO_PATH,
    png_path: Path = DEFAULT_PNG_PATH,
) -> tuple[Path, Path]:
    """Build multi-res Windows .ico & 256px+ PNG from Jesse in the Lab circular logo (with vector fallback)."""
    ico_path = Path(ico_path)
    png_path = Path(png_path)
    ico_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.parent.mkdir(parents=True, exist_ok=True)

    if brand.LOGO_PNG_PATH.is_file():
        with Image.open(brand.LOGO_PNG_PATH) as im:
            rgba = im.convert("RGBA")
            if png_path.resolve() != brand.LOGO_PNG_PATH.resolve():
                rgba.resize((512, 512), Image.Resampling.LANCZOS).save(png_path, format="PNG", optimize=True)
            rgba.resize((256, 256), Image.Resampling.LANCZOS).save(ico_path, format="ICO", sizes=ICO_SIZES)
        return ico_path, png_path

    # Vector circular fallback if master PNG is not yet provisioned
    size = 512
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    od = ImageDraw.Draw(canvas)
    od.ellipse((16, 16, size - 16, size - 16), fill=(11, 17, 32, 255), outline=(56, 189, 248, 255), width=16)
    cx = size // 2
    od.ellipse((cx - 110, cx - 110, cx + 110, cx + 110), outline=(248, 250, 252, 255), width=24)
    od.ellipse((cx - 40, cx - 40, cx + 40, cx + 40), fill=(56, 189, 248, 255))

    master = canvas.resize((256, 256), Image.Resampling.LANCZOS)
    master.save(png_path, format="PNG")
    master.save(ico_path, format="ICO", sizes=ICO_SIZES)
    return ico_path, png_path


def get_windows_desktop_dir() -> Path:
    """Resolve the active Windows Desktop folder path reliably."""
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                check=True,
                capture_output=True,
                text=True,
            )
            raw = proc.stdout.strip()
            if raw:
                return Path(raw)
        except Exception:
            pass
    return Path.home() / "Desktop"


def create_windows_shortcut(
    shortcut_path: Path,
    target_path: Path,
    arguments: str,
    working_dir: Path,
    icon_path: Path,
    description: str,
    window_style: int = 1,
) -> Path:
    """Create a Windows .lnk Desktop shortcut via WScript.Shell COM."""
    shortcut_path = Path(shortcut_path)
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    icon_location = f"{Path(icon_path)},0"

    ps_script = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "$ws = New-Object -ComObject WScript.Shell",
            f"$sc = $ws.CreateShortcut('{_ps_quote(str(shortcut_path))}')",
            f"$sc.TargetPath = '{_ps_quote(str(target_path))}'",
            f"$sc.Arguments = '{_ps_quote(arguments)}'",
            f"$sc.WorkingDirectory = '{_ps_quote(str(working_dir))}'",
            f"$sc.IconLocation = '{_ps_quote(icon_location)}'",
            f"$sc.Description = '{_ps_quote(description)}'",
            f"$sc.WindowStyle = {int(window_style)}",
            "$sc.Save()",
        ]
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        check=True,
        capture_output=True,
        text=True,
    )

    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
        except Exception:
            pass
    return shortcut_path


def install_desktop_shortcut(
    desktop_dir: Optional[Path] = None,
    port: int = brand.DEFAULT_PORT,
    shortcut_name: str = DEFAULT_SHORTCUT_NAME,
) -> dict[str, str]:
    """Generate Studio icon and install LabGuide Studio.lnk on Desktop."""
    ico_path, png_path = generate_studio_icon()
    target_dir = Path(desktop_dir) if desktop_dir is not None else get_windows_desktop_dir()
    shortcut_path = target_dir / shortcut_name

    python_exe = brand.get_venv_python()
    launcher_path = PROJECT_DIR / "launcher.py"
    arguments = f'"{launcher_path}" --port {int(port)}'

    create_windows_shortcut(
        shortcut_path=shortcut_path,
        target_path=python_exe,
        arguments=arguments,
        working_dir=WORKSPACE_ROOT,
        icon_path=ico_path,
        description="Start LabGuide Studio (Jesse in the Lab) dependencies and open Studio Web UI",
        window_style=1,
    )
    return {
        "shortcut": str(shortcut_path),
        "target": str(python_exe),
        "arguments": arguments,
        "working_dir": str(WORKSPACE_ROOT),
        "icon": str(ico_path),
        "icon_png": str(png_path),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Create LabGuide Studio Desktop shortcut and icon.")
    parser.add_argument("--desktop-dir", default=None, help="Override target Desktop directory")
    parser.add_argument("--port", type=int, default=brand.DEFAULT_PORT, help="Port number (default 8897)")
    parser.add_argument("--name", default=DEFAULT_SHORTCUT_NAME, help="Shortcut filename (.lnk)")
    args = parser.parse_args(argv)

    result = install_desktop_shortcut(
        desktop_dir=Path(args.desktop_dir) if args.desktop_dir else None,
        port=args.port,
        shortcut_name=args.name,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
