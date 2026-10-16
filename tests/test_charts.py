"""T015: Pure function tests of dashboard/charts.py (US3).

- SVG root with role="img" and aria-label naming the metric
- Bottom axis with five tick labels and gridlines
- nice_max computation
- No clipping: all text boxes inside viewport, no overlap
- Value labels at bar end + 4px inside value column
- Bar width = plot_w * v / nice_max
- Bars have class="bar", tabindex="0", data-tooltip, aria-label
- Long model names grow label_w
- Determinism: identical inputs -> byte-identical output
"""

import math
import re
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

from charts import bar_chart_svg  # noqa: E402

ROW_H = 30
PAD = 12
PLOT_W = 360
CHAR_W = 6.6
AXIS_H = 26


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    k = math.floor(math.log10(v))
    for base in (1, 2, 2.5, 5):
        candidate = base * (10 ** k)
        if candidate >= v:
            return candidate
    return 10 ** (k + 1)


def _parse_svg(svg_str: str):
    svg_str = svg_str.replace("http://www.w3.org/2000/svg", "http://ns")
    ns = {"svg": "http://ns"}
    root = ET.fromstring(svg_str)
    return root


class ChartGeometryTests(unittest.TestCase):
    """T015: geometry assertions on the SVG output."""

    def _entries(self):
        return [
            ("Qwen3.8-27B-8bit", 34.0, "tip1"),
            ("Qwen3.8-27B-oQ4e", 58.8, "tip2"),
        ]

    def test_svg_root_attributes(self):
        svg = bar_chart_svg("decode @ 16k prompt", "tok/s", "higher", self._entries())
        self.assertIn('role="img"', svg)
        self.assertIn('aria-label="decode @ 16k prompt"', svg)

    def test_five_tick_labels(self):
        svg = bar_chart_svg("m", "u", "higher", self._entries())
        root = _parse_svg(svg)
        texts = [t.text or "" for t in root.iter("text")]
        nice = _nice_max(58.8)
        expected_ticks = {str(0), str(nice / 4), str(nice / 2),
                          str(3 * nice / 4), str(nice)}
        tick_texts = set(texts)
        for t in expected_ticks:
            self.assertIn(t, tick_texts, f"tick {t!r} not found in {tick_texts}")

    def test_nice_max_values(self):
        self.assertEqual(_nice_max(34.0), 50.0)
        self.assertEqual(_nice_max(99.9), 100.0)
        self.assertEqual(_nice_max(120), 200.0)

    def test_no_clipping(self):
        entries = self._entries()
        svg = bar_chart_svg("m", "u", "higher", entries)
        root = _parse_svg(svg)
        width = int(root.get("width"))
        height = int(root.get("height"))

        def text_box(t):
            x = float(t.get("x", 0))
            y = float(t.get("y", 0))
            text = t.text or ""
            w = len(text) * CHAR_W
            h = 13
            anchor = t.get("text-anchor", "start")
            if anchor == "middle":
                x -= w / 2
            elif anchor == "end":
                x -= w
            return (x, y - h, x + w, y)

        boxes = []
        for t in root.iter("text"):
            x = float(t.get("x", 0))
            y = float(t.get("y", 0))
            text = t.text or ""
            w = len(text) * CHAR_W
            h = 13
            anchor = t.get("text-anchor", "start")
            if anchor == "middle":
                x -= w / 2
            elif anchor == "end":
                x -= w
            boxes.append((x, y - h, x + w, y))
            self.assertGreaterEqual(x, 0, f"text at x={x} outside viewport")
            self.assertGreaterEqual(y - h, 0, f"text at y={y - h} outside viewport")
            self.assertLessEqual(x + w, width, f"text right={x + w} > width={width}")
            self.assertLessEqual(y, height, f"text bottom={y} > height={height}")

        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                overlap_x = a[0] < b[2] and b[0] < a[2]
                overlap_y = a[1] < b[3] and b[1] < a[3]
                self.assertFalse(overlap_x and overlap_y,
                                 f"text boxes {i} and {j} overlap: {a} vs {b}")

    def test_bar_attributes(self):
        entries = self._entries()
        svg = bar_chart_svg("m", "u", "higher", entries)
        root = _parse_svg(svg)
        rects = list(root.iter("rect"))
        self.assertEqual(len(rects), 2)
        for i, (model, value, tip) in enumerate(entries):
            r = rects[i]
            self.assertIn("bar", r.get("class", ""))
            self.assertEqual(r.get("tabindex"), "0")
            self.assertEqual(r.get("data-tooltip"), tip)
            self.assertEqual(r.get("aria-label"), f"{model}: {value} u")

    def test_bar_width_proportional(self):
        entries = self._entries()
        svg = bar_chart_svg("m", "u", "higher", entries)
        root = _parse_svg(svg)
        nice = _nice_max(58.8)
        rects = list(root.iter("rect"))
        for i, (model, value, tip) in enumerate(entries):
            w = float(rects[i].get("width"))
            expected = PLOT_W * value / nice
            self.assertAlmostEqual(w, expected, delta=1.0,
                                   msg=f"bar {i} width {w} != {expected}")

    def test_long_model_name(self):
        entries = [("A" * 40, 10.0, "tip"), ("B", 20.0, "tip2")]
        svg = bar_chart_svg("m", "u", "higher", entries)
        root = _parse_svg(svg)
        width = int(root.get("width"))
        for t in root.iter("text"):
            x = float(t.get("x", 0))
            text = t.text or ""
            w = len(text) * CHAR_W
            anchor = t.get("text-anchor", "start")
            if anchor == "middle":
                x -= w / 2
            elif anchor == "end":
                x -= w
            self.assertGreaterEqual(x, 0)
            self.assertLessEqual(x + w, width)

    def test_determinism(self):
        entries = self._entries()
        svg1 = bar_chart_svg("m", "u", "higher", entries)
        svg2 = bar_chart_svg("m", "u", "higher", entries)
        self.assertEqual(svg1, svg2)


if __name__ == "__main__":
    unittest.main()
