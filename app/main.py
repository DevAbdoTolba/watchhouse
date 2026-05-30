"""Application entry point.

Creates the QApplication, enforces a single running instance (two copies would
record the same cameras and corrupt each other's footage), applies the dark
theme, shows the main window, and starts the Qt event loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QDir, QLockFile, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

from app.core.config import Settings
from app.core.log import bus
from app.ui import theme
from app.ui.main_window import MainWindow


def _qcolor(hex_str: str):
    from PySide6.QtGui import QColor
    return QColor(hex_str)


def _acquire_single_instance() -> QLockFile | None:
    """Return a held QLockFile, or None if another instance already runs.

    Two Watchhouse processes would each spawn ffmpeg recorders against the
    same cameras and write to the same segment files, corrupting each other's
    recordings (the user hit exactly this). `removeStaleLockFile()` clears a
    lock left by a crashed / force-killed prior run, so a clean relaunch still
    works immediately.
    """
    lock = QLockFile(str(Path(QDir.tempPath()) / "watchhouse.lock"))
    lock.setStaleLockTime(30_000)
    if lock.tryLock(100):
        return lock
    lock.removeStaleLockFile()  # owner gone? clear and retry once
    if lock.tryLock(100):
        return lock
    return None


def main() -> int:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
    app = QApplication(sys.argv)
    app.setApplicationName("Watchhouse")
    app.setOrganizationName("Watchhouse")
    app.setStyle("Fusion")

    lock = _acquire_single_instance()
    if lock is None:
        bus.warn("APP", "another Watchhouse instance is already running; exiting")
        QMessageBox.warning(
            None,
            "Watchhouse already running",
            "Watchhouse is already running.\n\n"
            "Only one instance can run at a time — two would record the same "
            "cameras and corrupt each other's footage.",
        )
        return 1
    app._instance_lock = lock  # hold for the whole process lifetime  # noqa: SLF001

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, _qcolor(theme.INK))
    palette.setColor(QPalette.ColorRole.WindowText, _qcolor(theme.TEXT))
    palette.setColor(QPalette.ColorRole.Base, _qcolor(theme.SURFACE))
    palette.setColor(QPalette.ColorRole.AlternateBase, _qcolor(theme.SURFACE_2))
    palette.setColor(QPalette.ColorRole.Text, _qcolor(theme.TEXT))
    palette.setColor(QPalette.ColorRole.Button, _qcolor(theme.SURFACE_2))
    palette.setColor(QPalette.ColorRole.ButtonText, _qcolor(theme.TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, _qcolor(theme.ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, _qcolor(theme.INK))
    app.setPalette(palette)
    app.setStyleSheet(theme.STYLESHEET)

    settings = Settings.load()
    window = MainWindow(settings)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
