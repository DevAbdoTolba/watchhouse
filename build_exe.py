"""Build Watchhouse.exe via PyInstaller (one-file, windowed).

Run from inside the project venv:
    pip install -e .[build]
    python build_exe.py

Output: dist/Watchhouse.exe
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent
ICON = ROOT / "app" / "resources" / "icon.ico"
ENTRY = ROOT / "app" / "__main__.py"


def main() -> int:
    if (ROOT / "build").exists():
        shutil.rmtree(ROOT / "build")
    if (ROOT / "dist").exists():
        shutil.rmtree(ROOT / "dist")

    # Heavy PySide6 modules we never import. Excluding shrinks the bundle
    # from ~250 MB down to ~80 MB.
    pyside_excludes = [
        "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
        "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
        "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets", "PySide6.QtNetworkAuth", "PySide6.QtNfc",
        "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf",
        "PySide6.QtPdfWidgets", "PySide6.QtPositioning", "PySide6.QtPrintSupport",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickControls2",
        "PySide6.QtQuickWidgets", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
        "PySide6.QtSensors", "PySide6.QtSerialBus", "PySide6.QtSerialPort",
        "PySide6.QtSpatialAudio", "PySide6.QtSql", "PySide6.QtStateMachine",
        "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtTest", "PySide6.QtTextToSpeech",
        "PySide6.QtUiTools", "PySide6.QtVirtualKeyboard", "PySide6.QtWebChannel",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick", "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebSockets", "PySide6.QtWebView", "PySide6.QtXml",
    ]

    args = [
        str(ENTRY),
        "--name=Watchhouse",
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--collect-binaries=cv2",
        "--collect-data=cv2",
        # Recorder needs the bundled ffmpeg executable from imageio-ffmpeg.
        "--collect-binaries=imageio_ffmpeg",
        "--collect-data=imageio_ffmpeg",
        f"--paths={ROOT}",
        # numpy.random.bit_generator imports `secrets` from a Cython module,
        # which PyInstaller's static analysis cannot see.
        "--hidden-import=secrets",
        "--exclude-module=tkinter",
        "--exclude-module=unittest",
        "--exclude-module=pydoc",
        "--exclude-module=test",
        "--exclude-module=xmlrpc",
    ]
    for mod in pyside_excludes:
        args.append(f"--exclude-module={mod}")
    if ICON.exists():
        args.append(f"--icon={ICON}")

    PyInstaller.__main__.run(args)

    out = ROOT / "dist" / "Watchhouse.exe"
    if out.exists():
        size_mb = out.stat().st_size / (1024 * 1024)
        print()
        print(f"Built {out}  ({size_mb:.1f} MB)")
        return 0
    print("Build failed: dist\\Watchhouse.exe not produced", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
