"""SettingsDialog round-trip checks: every tab must surface the values it
was given and hand back edits through its getters — the contract
MainWindow's apply step relies on. Offscreen, real widgets.

Stdlib unittest only; run with:  python -m unittest discover -s tests
"""

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Applied per-setUp (not at import) so they can't leak into sibling test
# modules — unittest imports everything before running anything.
_ENV = {
    "RECORDING_ENABLED": "0",
    "WATCHDOG_ENABLED": "0",
    "TELEGRAM_BOT_TOKEN": "111:AAA-test-token",
    "TELEGRAM_CHAT_ID": "-100123",
    "TELEGRAM_COMMANDS": "1",
    "TELEGRAM_LANG": "ar",
    "WIPE_PIN": "9876",
}

from PySide6.QtWidgets import QApplication

from app.core.cameras import default_cameras
from app.core.camera_links import Link
from app.core.config import Settings
from app.ui.settings_dialog import SettingsDialog


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class SettingsDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.update(_ENV)

    def _dialog(self) -> SettingsDialog:
        settings = Settings.load()
        self.assertEqual(settings.wipe_pin, "9876")  # .env override honoured
        link = Link(name="Front door", cam_a=1, edge_a="left", cam_b=3,
                    edge_b="right", transit_s=4.0,
                    label_ab="went out", label_ba="came in")
        return SettingsDialog(
            default_cameras(), settings,
            names={2: "Entry"}, floors={4: 0.78}, links=[link],
            cam_labels={2: "Entry"}, watchdog_on=True,
        )

    def test_round_trips_initial_values(self) -> None:
        dlg = self._dialog()
        self.assertEqual(dlg.cameras_page.names(), {2: "Entry"})
        floors = dlg.detection_page.values()
        self.assertEqual(floors[4], 0.78)
        self.assertEqual(floors[1], 0.0)  # uncapped cameras come back as 0
        token, chat, commands, lang = dlg.telegram_page.values()
        self.assertEqual((token, chat, commands, lang),
                         ("111:AAA-test-token", "-100123", True, "ar"))
        links = dlg.links_page.values()
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].name, "Front door")
        self.assertTrue(dlg.system_page.watchdog_enabled())

    def test_edits_come_back_through_getters(self) -> None:
        dlg = self._dialog()
        dlg.cameras_page._inputs[1].setText("  Gate  cam  ")
        self.assertEqual(dlg.cameras_page.names()[1], "Gate cam")  # cleaned
        dlg.detection_page._spins[2].setValue(0.55)
        self.assertEqual(dlg.detection_page.values()[2], 0.55)
        dlg.system_page._wd_chk.setChecked(False)
        self.assertFalse(dlg.system_page.watchdog_enabled())
        # Links editor: add a second link through the real form.
        page = dlg.links_page
        page._name.setText("Side gate")
        page._cam_a.setCurrentIndex(1)   # cam 2
        page._cam_b.setCurrentIndex(3)   # cam 4
        page._on_add_update()
        self.assertEqual([lk.name for lk in page.values()],
                         ["Front door", "Side gate"])


if __name__ == "__main__":
    unittest.main()
