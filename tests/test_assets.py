"""T004: Verify the v2 design system constants in dashboard/assets.py.

- COLORS has required keys with WCAG AA contrast
- STYLE and SCRIPTS are non-empty
- No external references in either string
- STYLE contains :focus-visible and prefers-reduced-motion
- SCRIPTS contains no forbidden APIs
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

from assets import COLORS, SCRIPTS, STYLE  # noqa: E402


def _relative_luminance(hex_color: str) -> float:
    """Compute WCAG relative luminance from a #rrggbb hex string."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0

    def linearize(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = linearize(r), linearize(g), linearize(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg: str, bg: str) -> float:
    """WCAG contrast ratio between two hex colours."""
    l1 = _relative_luminance(fg)
    l2 = _relative_luminance(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


class ColorsContrastTests(unittest.TestCase):
    """T004(a)(b): palette keys and WCAG AA contrast ratios."""

    def test_required_keys_present(self):
        for key in ("bg", "text", "secondary", "link"):
            self.assertIn(key, COLORS, f"COLORS missing key {key!r}")

    def test_badge_pairs_present(self):
        for status in ("shipped", "on-trial", "retired", "dead-end", "open"):
            self.assertIn(f"badge-{status}-text", COLORS,
                          f"COLORS missing badge-{status}-text")
            self.assertIn(f"badge-{status}-bg", COLORS,
                          f"COLORS missing badge-{status}-bg")

    def test_body_contrast(self):
        ratio = _contrast_ratio(COLORS["text"], COLORS["bg"])
        self.assertGreaterEqual(ratio, 4.5,
                                f"text/bg contrast {ratio:.2f} < 4.5")

    def test_secondary_contrast(self):
        ratio = _contrast_ratio(COLORS["secondary"], COLORS["bg"])
        self.assertGreaterEqual(ratio, 4.5,
                                f"secondary/bg contrast {ratio:.2f} < 4.5")

    def test_link_contrast(self):
        ratio = _contrast_ratio(COLORS["link"], COLORS["bg"])
        self.assertGreaterEqual(ratio, 4.5,
                                f"link/bg contrast {ratio:.2f} < 4.5")

    def test_badge_contrast(self):
        for status in ("shipped", "on-trial", "retired", "dead-end", "open"):
            fg = COLORS[f"badge-{status}-text"]
            bg = COLORS[f"badge-{status}-bg"]
            ratio = _contrast_ratio(fg, bg)
            self.assertGreaterEqual(ratio, 4.5,
                                    f"badge-{status} contrast {ratio:.2f} < 4.5")

    def test_v1_secondary_gone(self):
        self.assertNotIn("#777", COLORS.values(),
                         "v1 #777 secondary must be replaced")


class StyleScriptPresenceTests(unittest.TestCase):
    """T004(c)(d)(e)(f): non-empty, no external refs, a11y rules, no forbidden JS."""

    def test_style_non_empty(self):
        self.assertTrue(STYLE.strip(), "STYLE is empty")

    def test_scripts_non_empty(self):
        self.assertTrue(SCRIPTS.strip(), "SCRIPTS is empty")

    def test_no_external_refs_in_style(self):
        for pat in ("http://", "https://", 'src="', "@import", "url("):
            self.assertNotIn(pat, STYLE, f"STYLE contains external ref {pat!r}")

    def test_no_external_refs_in_scripts(self):
        for pat in ("http://", "https://", 'src="', "@import", "url("):
            self.assertNotIn(pat, SCRIPTS, f"SCRIPTS contains external ref {pat!r}")

    def test_focus_visible(self):
        self.assertIn(":focus-visible", STYLE)

    def test_reduced_motion(self):
        self.assertIn("@media (prefers-reduced-motion: reduce)", STYLE)

    def test_scripts_no_forbidden_apis(self):
        forbidden = [
            "fetch(", "XMLHttpRequest", "WebSocket", "import(",
            "localStorage", "sessionStorage", "indexedDB",
            "document.cookie", "history.pushState", "history.replaceState",
            "new Date(",
        ]
        for pat in forbidden:
            self.assertNotIn(pat, SCRIPTS,
                             f"SCRIPTS contains forbidden API {pat!r}")


if __name__ == "__main__":
    unittest.main()
