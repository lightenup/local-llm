"""dashboard/markdown.py: the FINDINGS.md converter."""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

from findings import load_sections  # noqa: E402
from markdown import inline, render_markdown  # noqa: E402


class BlockTests(unittest.TestCase):
    def html(self, md):
        return render_markdown(md).html

    def test_headings_get_github_ids_with_duplicate_suffixes(self):
        r = render_markdown("## 3. ANE offload — rejected\n\n### Notes\n\n### Notes\n")
        self.assertEqual([h.id for h in r.headings],
                         ["3-ane-offload--rejected", "notes", "notes-1"])

    def test_fenced_and_indented_code_are_escaped_and_not_parsed(self):
        h = self.html("```\n# not a heading <b>\n```\n\npara\n\n    **raw** <x>\n")
        self.assertIn("<pre><code># not a heading &lt;b&gt;</code></pre>", h)
        self.assertIn("<pre><code>**raw** &lt;x&gt;</code></pre>", h)
        self.assertNotIn("<h1", h)

    def test_table_with_alignment(self):
        h = self.html("| a | b |\n|---|--:|\n| `x|y` | **2** |\n")
        self.assertIn('<th style="text-align:right">b</th>', h)
        self.assertIn("<strong>2</strong>", h)

    def test_lists_nest_and_numbered_text_is_not_a_list(self):
        h = self.html("- one\n  - inner\n- two\n\nexit\n200) while it ran\n")
        self.assertEqual(h.count("<ul>"), 2)
        self.assertNotIn("<ol", h)

    def test_quote_and_rule(self):
        h = self.html("> quoted *text*\n\n---\n")
        self.assertIn("<blockquote><p>quoted <em>text</em></p></blockquote>", h)
        self.assertIn("<hr>", h)


class InlineTests(unittest.TestCase):
    def test_code_spans_are_literal(self):
        self.assertEqual(inline("`a*b*`"), "<code>a*b*</code>")

    def test_section_refs_link_only_known_sections(self):
        out = inline("see §3 and §99", {3: "3-x"}, "findings.html")
        self.assertIn('<a href="findings.html#3-x">§3</a>', out)
        self.assertIn("§99", out)
        self.assertNotIn("#99", out)

    def test_html_is_escaped(self):
        self.assertEqual(inline("<script>"), "&lt;script&gt;")

    def test_underscores_and_products_stay_text(self):
        self.assertEqual(inline("snake_case 2*3*4"), "snake_case 2*3*4")


class RealDocumentTests(unittest.TestCase):
    def test_every_numbered_section_id_matches_the_link_slug(self):
        md = (ROOT / "docs" / "FINDINGS.md").read_text()
        ids = {h.id for h in render_markdown(md).headings}
        for num, (_, anchor) in load_sections(ROOT / "docs" / "FINDINGS.md").items():
            self.assertIn(anchor, ids, f"§{num}")

    def test_ids_are_unique(self):
        r = render_markdown((ROOT / "docs" / "FINDINGS.md").read_text())
        ids = [h.id for h in r.headings]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()


class ReviewRegressionTests(unittest.TestCase):
    def test_link_url_is_escaped_once(self):
        self.assertIn('href="http://a?b=1&amp;c=2"', inline("[x](http://a?b=1&c=2)"))

    def test_section_ref_inside_link_text_is_not_nested(self):
        out = inline("[see §3](#x) and §3", {3: "s3"})
        self.assertEqual(out.count("<a "), 2)
        self.assertNotIn("<a href=\"#x\"><a", out)

    def test_heading_after_a_list_is_not_swallowed(self):
        r = render_markdown("- a\n- b\n## 5. Heading\npara\n")
        self.assertEqual([h.id for h in r.headings], ["5-heading"])
        self.assertIn("<p>para</p>", r.html)
