"""Markdown -> HTML for docs/FINDINGS.md, standard library only.

Covers what FINDINGS.md uses and nothing more: ATX headings, fenced code,
pipe tables, - / * / 1. lists (nested by indent), > quotes, --- rules and
paragraphs; inline **bold**, *em*, `code`, [text](url) and §N. Anything else
is escaped and shown as text, so an unsupported construct degrades to its
source rather than to broken markup.

Heading ids use findings.slug with GitHub's duplicate suffixes (-1, -2), the
same ids the dashboard links to, so a link and its target cannot drift.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from findings import slug

_FENCE = re.compile(r"^(```|~~~)")
_HEADING = re.compile(r"^(#{1,6}) +(.+?) *#*$")
_HR = re.compile(r"^ {0,3}([-*_])( *\1){2,} *$")
_ITEM = re.compile(r"^( *)([-*+]|\d+[.)]) +(.*)$")
_TABLE_SEP = re.compile(r"^ *\|? *:?-+:? *(\| *:?-+:? *)*\|? *$")
_SECTION_REF = re.compile(r"§(\d+)")
_INDENTED = re.compile(r"^(    |\t)")


def _interrupts(line: str) -> bool:
    """CommonMark: only a bullet, or an ordered item numbered 1, may start a
    list in the middle of a paragraph ("200) while ..." is text)."""
    m = _ITEM.match(line)
    return bool(m) and (not m.group(2)[0].isdigit() or m.group(2)[:-1] == "1")


@dataclass
class Heading:
    level: int
    text: str
    id: str


@dataclass
class Rendered:
    html: str
    headings: list[Heading] = field(default_factory=list)


def link_sections(escaped: str, section_ids: dict[int, str], href: str = "") -> str:
    """Turn §N in already-escaped text into links. Unknown N stays text."""
    def sub(m: re.Match) -> str:
        sid = section_ids.get(int(m.group(1)))
        return f'<a href="{href}#{sid}">§{m.group(1)}</a>' if sid else m.group(0)
    return _SECTION_REF.sub(sub, escaped)


def inline(text: str, section_ids: dict[int, str] | None = None,
           section_href: str = "", link_map=None) -> str:
    """Inline markup. Code spans are cut out first so nothing inside them is
    interpreted; then the rest is escaped and marked up."""
    parts = re.split(r"(`+)(.+?)\1", text)
    out = []
    # re.split with two groups yields: text, ticks, code, text, ticks, code, ...
    for i in range(0, len(parts), 3):
        out.append(_inline_text(parts[i], section_ids, section_href, link_map))
        if i + 2 < len(parts):
            out.append(f"<code>{html.escape(parts[i + 2].strip(), quote=False)}</code>")
    return "".join(out)


def _inline_text(t: str, section_ids, section_href, link_map) -> str:
    t = html.escape(t, quote=False)

    def link(m: re.Match) -> str:
        url = m.group(2)
        if link_map:
            url = link_map(url)
        return f'<a href="{html.escape(url, quote=True)}">{m.group(1)}</a>'
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", t)
    if section_ids:
        t = link_sections(t, section_ids, section_href)
    return t


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    cells = re.split(r"(?<!\\)\|", row)
    return [c.strip().replace("\\|", "|") for c in cells]


def _align(sep: str) -> list[str]:
    out = []
    for c in _cells(sep):
        left, right = c.startswith(":"), c.endswith(":")
        out.append("center" if left and right else "right" if right else "")
    return out


class _Renderer:
    def __init__(self, section_ids, section_href, link_map):
        self.section_ids = section_ids
        self.section_href = section_href
        self.link_map = link_map
        self.headings: list[Heading] = []
        self._seen: dict[str, int] = {}

    def il(self, t: str) -> str:
        return inline(t, self.section_ids, self.section_href, self.link_map)

    def heading_id(self, text: str) -> str:
        base = slug(text)
        n = self._seen.get(base, 0)
        self._seen[base] = n + 1
        return base if n == 0 else f"{base}-{n}"

    def blocks(self, lines: list[str]) -> str:
        out: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            m = _FENCE.match(line.strip())
            if m:
                fence = m.group(1)
                j = i + 1
                while j < n and not lines[j].strip().startswith(fence):
                    j += 1
                code = "\n".join(lines[i + 1:j])
                out.append(f"<pre><code>{html.escape(code, quote=False)}</code></pre>")
                i = j + 1
                continue
            m = _HEADING.match(line)
            if m:
                level, text = len(m.group(1)), m.group(2)
                hid = self.heading_id(text)
                self.headings.append(Heading(level, text, hid))
                out.append(f'<h{level} id="{hid}">{self.il(text)}</h{level}>')
                i += 1
                continue
            if _INDENTED.match(line):
                j = i
                code = []
                while j < n and (_INDENTED.match(lines[j]) or not lines[j].strip()):
                    code.append(lines[j][4:] if lines[j].startswith("    ") else lines[j].lstrip("\t"))
                    j += 1
                while code and not code[-1].strip():
                    code.pop()
                out.append(f"<pre><code>{html.escape(chr(10).join(code), quote=False)}</code></pre>")
                i = j
                continue
            if _HR.match(line):
                out.append("<hr>")
                i += 1
                continue
            if line.lstrip().startswith(">"):
                j = i
                quoted = []
                while j < n and lines[j].lstrip().startswith(">"):
                    quoted.append(re.sub(r"^ *> ?", "", lines[j]))
                    j += 1
                out.append(f"<blockquote>{self.blocks(quoted)}</blockquote>")
                i = j
                continue
            if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
                head = _cells(line)
                align = _align(lines[i + 1])
                j = i + 2
                rows = []
                while j < n and lines[j].strip() and "|" in lines[j]:
                    rows.append(_cells(lines[j]))
                    j += 1
                out.append(self.table(head, align, rows))
                i = j
                continue
            if _ITEM.match(line):
                j, items = self.collect_list(lines, i)
                out.append(items)
                i = j
                continue
            j = i
            para = []
            while (j < n and lines[j].strip() and not _FENCE.match(lines[j].strip())
                   and not _HEADING.match(lines[j]) and not _HR.match(lines[j])
                   and not lines[j].lstrip().startswith(">")
                   and not (j > i and _interrupts(lines[j]))):
                para.append(lines[j].strip())
                j += 1
            out.append(f"<p>{self.il(' '.join(para))}</p>")
            i = j
        return "\n".join(out)

    def table(self, head, align, rows) -> str:
        def cell(tag, c, k):
            a = align[k] if k < len(align) else ""
            style = f' style="text-align:{a}"' if a else ""
            return f"<{tag}{style}>{self.il(c)}</{tag}>"
        th = "".join(cell("th", c, k) for k, c in enumerate(head))
        body = "".join("<tr>" + "".join(cell("td", c, k) for k, c in enumerate(r)) + "</tr>"
                       for r in rows)
        return f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'

    def collect_list(self, lines: list[str], i: int) -> tuple[int, str]:
        first = _ITEM.match(lines[i])
        indent = len(first.group(1))
        ordered = first.group(2)[0].isdigit()
        items: list[list[str]] = []
        n = len(lines)
        j = i
        while j < n:
            line = lines[j]
            m = _ITEM.match(line)
            if m and len(m.group(1)) == indent:
                if m.group(2)[0].isdigit() != ordered:
                    break
                items.append([m.group(3)])
                j += 1
                continue
            if not line.strip():
                # a blank line ends the list unless the next line continues it
                k = j + 1
                if k < n and (lines[k].startswith(" " * (indent + 2))
                              or (_ITEM.match(lines[k]) and len(_ITEM.match(lines[k]).group(1)) == indent)):
                    items[-1].append("")
                    j += 1
                    continue
                break
            if line.startswith(" " * (indent + 1)) or (not _ITEM.match(line) and items):
                if _ITEM.match(line) or line.startswith(" " * (indent + 1)):
                    items[-1].append(line[indent + 2:] if line.startswith(" " * (indent + 2)) else line.strip())
                else:
                    items[-1].append(line.strip())
                j += 1
                continue
            break
        tag = "ol" if ordered else "ul"
        start = ""
        if ordered:
            num = int(re.match(r"\d+", first.group(2)).group(0))
            if num != 1:
                start = f' start="{num}"'
        lis = []
        for body in items:
            if len(body) == 1 or not any(_ITEM.match(b) or _FENCE.match(b.strip()) or not b.strip()
                                         for b in body[1:]):
                lis.append(f"<li>{self.il(' '.join(b.strip() for b in body))}</li>")
            else:
                inner = self.blocks(body)
                # a tight item's first paragraph reads better unwrapped
                if inner.startswith("<p>"):
                    end = inner.find("</p>")
                    inner = inner[3:end] + inner[end + 4:]
                lis.append(f"<li>{inner}</li>")
        return j, f"<{tag}{start}>" + "".join(lis) + f"</{tag}>"


def render_markdown(text: str, section_ids: dict[int, str] | None = None,
                    section_href: str = "", link_map=None) -> Rendered:
    r = _Renderer(section_ids, section_href, link_map)
    body = r.blocks(text.splitlines())
    return Rendered(body, r.headings)
