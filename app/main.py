"""Application entry point."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from app.core.config import Settings
from app.ui import theme
from app.ui.main_window import MainWindow


def main() -> int:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
    app = QApplication(sys.argv)
    app.setApplicationName("CCTV Console")
    app.setOrganizationName("HomeCameras")
    app.setStyle("Fusion")

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


def _qcolor(hex_str: str):
    from PySide6.QtGui import QColor
    return QColor(hex_str)


if __name__ == "__main__":
    raise SystemExit(main())
