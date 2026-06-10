"""footage_spans behavioral checks: the pure span math behind the timeline's
blue (pinned/kept) layer and the lock-only-real-footage rule.

Stdlib unittest only; run with:  python -m unittest discover -s tests
"""

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.core import footage_spans


def _clip(path: str, start: datetime, dur_s: float) -> SimpleNamespace:
    return SimpleNamespace(path=Path(path), start_at=start,
                           end_at_estimated=start + timedelta(seconds=dur_s))


class _FakePins:
    def __init__(self, spans):
        self._spans = spans

    def all_spans(self, _now):
        return self._spans


class RecordedSubrangesTests(unittest.TestCase):
    def test_clamps_to_window_and_skips_gaps(self) -> None:
        t0 = datetime(2026, 6, 10, 12, 0, 0)
        library = {
            1: [_clip("D:/r/cam1/a.mp4", t0, 180),                       # 12:00-12:03
                _clip("D:/r/cam1/b.mp4", t0 + timedelta(minutes=10), 180)],  # 12:10-12:13
        }
        # Window 12:02–12:11 overlaps both clips but not the gap between them.
        start, end = t0 + timedelta(minutes=2), t0 + timedelta(minutes=11)
        subs = footage_spans.recorded_subranges(library, start, end)
        self.assertEqual(len(subs), 2)
        self.assertEqual(subs[0], (start, t0 + timedelta(minutes=3)))
        self.assertEqual(subs[1], (t0 + timedelta(minutes=10), end))

    def test_empty_window_yields_nothing(self) -> None:
        t0 = datetime(2026, 6, 10, 12, 0, 0)
        library = {1: [_clip("D:/r/cam1/a.mp4", t0, 60)]}
        subs = footage_spans.recorded_subranges(
            library, t0 + timedelta(hours=1), t0 + timedelta(hours=2))
        self.assertEqual(subs, [])


class PinnedSpansTests(unittest.TestCase):
    def test_imported_per_cam_and_pins_for_all(self) -> None:
        t0 = datetime(2026, 6, 10, 9, 0, 0)
        library = {
            0: [_clip("D:/rec/imported/x.mp4", t0, 60)],   # imported -> cam 0 only
            1: [_clip("D:/rec/cam1/a.mp4", t0, 60)],       # normal -> not blue
        }
        pin_span = (t0 + timedelta(hours=1), t0 + timedelta(hours=2))
        cams = [SimpleNamespace(index=1), SimpleNamespace(index=2)]
        out = footage_spans.pinned_spans_by_cam(
            library, _FakePins([pin_span]), cams, Path("D:/rec/imported"))
        # The imported clip is blue on its own camera…
        self.assertEqual(out[0], [(t0, t0 + timedelta(seconds=60))])
        # …the pinned range applies to every configured camera…
        self.assertIn(pin_span, out[1])
        self.assertIn(pin_span, out[2])
        # …and a normal recording outside imported/ is not blue by itself.
        self.assertNotIn((t0, t0 + timedelta(seconds=60)), out.get(1, []))

    def test_no_pins_no_imports_is_empty(self) -> None:
        out = footage_spans.pinned_spans_by_cam(
            {}, _FakePins([]), [SimpleNamespace(index=1)], Path("D:/rec/imported"))
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
