"""Render Discourse ``cooked`` HTML as Rich renderables."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Iterable

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.rule import Rule
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .dom import Node, parse

if TYPE_CHECKING:
    from ..site import Site

STYLES = {
    "link": "underline #7fb2ff",
    "mention": "bold #e5c07b",
    "hashtag": "#c3a6ff",
    "code": "#e6e6e6 on #2b2b2b",
    "quote_bar": "#5f87af",
    "quote_head": "bold #9cb7d4",
    "onebox_bar": "#585858",
    "image": "#8fbf8f",
    "spoiler": "#444444 on #444444",
    "muted": "#8a8a8a",
    "heading": "bold #ffffff",
    "date": "#d7af87",
    "poll_bar": "#5f87af",
}

BLOCK = {
    "p", "div", "aside", "blockquote", "ul", "ol", "li", "pre", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "table", "details", "summary", "article", "header",
    "section", "figure", "dl", "iframe",
}
# Discourse wraps images in divs that sit inside paragraphs; keep them inline.
INLINE_DIVS = {"lightbox-wrapper", "d-image-grid"}
SKIP_CLASSES = {"quote-controls", "meta", "onebox-metadata", "github-row-stats"}
SKIP_IMG_CLASSES = {"avatar", "site-icon", "thumbnail", "onebox-avatar", "onebox-avatar-inline"}

_WS = re.compile(r"\s+")
_GUESSABLE = {
    "python", "bash", "json", "javascript", "java", "kotlin", "xml", "yaml", "ini",
    "c", "cpp", "html", "css", "sql", "powershell", "batch", "typescript", "go", "rust",
}


@dataclass
class Rendered:
    body: RenderableType
    links: list[tuple[str, str]] = field(default_factory=list)
    code_blocks: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    has_spoiler: bool = False
    has_details: bool = False


class Prefixed:
    """Render ``inner`` narrower and put ``first`` / ``rest`` in front of each line."""

    def __init__(self, inner: RenderableType, first: Text | str, rest: Text | str | None = None) -> None:
        self.inner = inner
        self.first = Text(first) if isinstance(first, str) else first
        if rest is None:
            rest = self.first
        self.rest = Text(rest) if isinstance(rest, str) else rest

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        pad = max(self.first.cell_len, self.rest.cell_len)
        width = max(1, options.max_width - pad)
        lines = console.render_lines(self.inner, options.update(width=width, height=None), pad=False)
        first = list(self.first.render(console))
        rest = list(self.rest.render(console))
        for i, line in enumerate(lines):
            yield from first if i == 0 else rest
            yield from line
            yield Segment.line()


def _bar(style: str, char: str = "▎") -> Text:
    return Text(char + " ", style=style)


def _stack(blocks: list[RenderableType], gap: int = 1) -> RenderableType:
    if not blocks:
        return Text("")
    if len(blocks) == 1:
        return blocks[0]
    out: list[RenderableType] = []
    for i, b in enumerate(blocks):
        if i and gap:
            out.append(Text(""))
        out.append(b)
    return Group(*out)


def _strip_text(t: Text) -> Text:
    t.rstrip()
    plain = t.plain
    lead = len(plain) - len(plain.lstrip())
    return t[lead:] if lead else t


class Renderer:
    def __init__(
        self,
        site: "Site | None",
        base_url: str,
        *,
        post: dict | None = None,
        topic_id: int | None = None,
        reveal_spoilers: bool = False,
        expand_details: bool = False,
    ) -> None:
        self.site = site
        self.base_url = base_url.rstrip("/")
        self.post = post or {}
        self.topic_id = topic_id
        self.reveal = reveal_spoilers
        self.expand = expand_details
        self.out = Rendered(Text(""))

    # --- helpers ---

    def _abs(self, url: str) -> str:
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return self.base_url + url
        return url

    def _link_style(self, url: str) -> Style:
        return Style.parse(STYLES["link"]) + Style(link=url)

    def _add_link(self, label: str, url: str) -> None:
        url = self._abs(url)
        if url and not url.startswith(("mailto:", "#")):
            self.out.links.append((label.strip() or url, url))

    def _emoji(self, name: str) -> Text:
        if self.site is not None:
            return self.site.emoji.rich(name)
        return Text(f":{name.strip(':')}:")

    # --- inline ---

    def _append(self, buf: Text, s: str, style: Style | str | None = None) -> None:
        s = _WS.sub(" ", s)
        if not s:
            return
        if s.startswith(" ") and (not buf.plain or buf.plain[-1] in " \n"):
            s = s[1:]
        if s:
            buf.append(s, style=style)

    def inline(self, node: Node | str, buf: Text, style: Style | None = None) -> None:
        if isinstance(node, str):
            self._append(buf, node, style)
            return
        tag = node.tag
        cls = node.classes
        if cls & SKIP_CLASSES or tag in ("svg", "script", "style", "use", "path"):
            return
        if tag == "br":
            buf.rstrip()
            buf.append("\n")
            return
        if tag == "img":
            self._inline_img(node, buf)
            return
        if "lightbox-wrapper" in cls:
            self._lightbox(node, buf)
            return
        if "spoiler" in cls:
            self.out.has_spoiler = True
            if not self.reveal:
                hidden = _WS.sub(" ", node.text()).strip()
                self._append(buf, " " + "█" * max(3, min(len(hidden), 24)) + " ", STYLES["spoiler"])
                return
        if "discourse-local-date" in cls:
            self._append(buf, self._local_date(node), STYLES["date"])
            return
        if "mention" in cls or "mention-group" in cls:
            self._append(buf, node.text(), STYLES["mention"])
            return
        if "hashtag-cooked" in cls or "badge-category__wrapper" in cls:
            label = _WS.sub(" ", node.text()).strip()
            if not label.startswith("#"):
                label = "#" + label
            self._append(buf, label, STYLES["hashtag"])
            return
        new = style
        if tag in ("strong", "b"):
            new = (style or Style()) + Style(bold=True)
        elif tag in ("em", "i"):
            new = (style or Style()) + Style(italic=True)
        elif tag == "del":
            # Edit-history diffs use ins/del; color them like the web view.
            new = (style or Style()) + Style.parse("strike #ff8787 on #3a1f1f")
        elif tag == "ins":
            new = (style or Style()) + Style.parse("#87d787 on #1f3a1f")
        elif tag in ("s", "strike"):
            new = (style or Style()) + Style(strike=True)
        elif tag == "u":
            new = (style or Style()) + Style(underline=True)
        elif tag == "small":
            new = (style or Style()) + Style(dim=True)
        elif tag in ("mark",):
            new = (style or Style()) + Style(reverse=True)
        elif tag == "kbd":
            new = (style or Style()) + Style(reverse=True, bold=True)
        elif tag == "code":
            buf.append(node.text(), style=STYLES["code"])
            return
        elif tag == "a":
            href = node.get("href")
            if href:
                url = self._abs(href)
                self._add_link(node.text(), url)
                new = (style or Style()) + self._link_style(url)
        for c in node.children:
            self.inline(c, buf, new)

    def _inline_img(self, node: Node, buf: Text) -> None:
        cls = node.classes
        if "emoji" in cls:
            name = node.get("title") or node.get("alt")
            buf.append_text(self._emoji(name))
            return
        if cls & SKIP_IMG_CLASSES:
            return
        src = self._abs(node.get("src"))
        alt = node.get("alt") or "image"
        self.out.images.append(src)
        self._append(buf, f" 🖼 {alt} ", Style.parse(STYLES["image"]) + Style(link=src))

    def _lightbox(self, node: Node, buf: Text) -> None:
        a = node.find("a", "lightbox") or node.find("a")
        href = self._abs(a.get("href")) if a else ""
        img = node.find("img")
        name_n = node.find(cls="filename")
        info_n = node.find(cls="informations")
        name = (name_n.text() if name_n else "") or (img.get("alt") if img else "") or "image"
        info = _WS.sub(" ", info_n.text()).strip() if info_n else ""
        label = f"🖼 {name}" + (f" · {info}" if info else "")
        if href:
            self.out.images.append(href)
        if buf.plain and buf.plain[-1] not in " \n":
            buf.append(" ")
        buf.append(label, style=Style.parse(STYLES["image"]) + (Style(link=href) if href else Style()))
        buf.append(" ")

    def _local_date(self, node: Node) -> str:
        date = node.get("data-date")
        time = node.get("data-time")
        tz = node.get("data-timezone") or "UTC"
        if not date:
            return node.text()
        try:
            from zoneinfo import ZoneInfo

            stamp = f"{date}T{time or '00:00:00'}"
            dt = datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo(tz)).astimezone()
        except Exception:  # noqa: BLE001
            return node.text() or date
        if time:
            return dt.strftime("%a %b %-d, %-I:%M %p")
        return dt.strftime("%a %b %-d, %Y")

    # --- blocks ---

    def blocks(self, node: Node) -> list[RenderableType]:
        out: list[RenderableType] = []
        buf = Text()

        def flush() -> None:
            nonlocal buf
            t = _strip_text(buf)
            if t.plain.strip():
                out.append(t)
            buf = Text()

        for c in node.children:
            if isinstance(c, Node) and c.tag in BLOCK and not (c.classes & INLINE_DIVS):
                if c.classes & SKIP_CLASSES:
                    continue
                flush()
                out.extend(self.block(c))
            else:
                self.inline(c, buf)
        flush()
        return out

    def block(self, node: Node) -> list[RenderableType]:
        tag = node.tag
        cls = node.classes
        if tag == "p" or tag in ("header", "article", "section", "figure", "dl"):
            return self.blocks(node)
        if tag == "div":
            if "spoiler" in cls:
                return self._spoiler_block(node)
            if "poll" in cls:
                return self._poll(node)
            if "math" in cls:
                return [Text(node.text().strip(), style="italic")]
            return self.blocks(node)
        if tag == "aside":
            if "quote" in cls:
                return [self._quote(node)]
            if "onebox" in cls:
                return [self._onebox(node)]
            return self.blocks(node)
        if tag == "blockquote":
            return [Prefixed(_stack(self.blocks(node)), _bar(STYLES["quote_bar"]))]
        if tag in ("ul", "ol"):
            return [self._list(node)]
        if tag == "li":
            return [_stack(self.blocks(node), gap=0)]
        if tag == "pre":
            return [self._code(node)]
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            buf = Text()
            for c in node.children:
                self.inline(c, buf, Style.parse(STYLES["heading"]))
            t = _strip_text(buf)
            if tag in ("h1", "h2"):
                t.stylize("underline")
            return [t]
        if tag == "hr":
            return [Rule(style=STYLES["muted"])]
        if tag == "table":
            return [self._table(node)]
        if tag == "details":
            return self._details(node)
        if tag == "iframe":
            src = self._abs(node.get("src"))
            self._add_link("embed", src)
            return [Text(f"▶ embed {src}", style=STYLES["muted"])]
        return self.blocks(node)

    def _spoiler_block(self, node: Node) -> list[RenderableType]:
        self.out.has_spoiler = True
        if self.reveal:
            return [Prefixed(_stack(self.blocks(node)), _bar(STYLES["muted"], "┊"))]
        return [Text("█████ spoiler, press S to reveal", style=STYLES["muted"])]

    def _quote(self, node: Node) -> RenderableType:
        user = node.get("data-username")
        post_no = node.get("data-post")
        topic = node.get("data-topic")
        head = Text()
        if user:
            head.append(f"@{user}", style=STYLES["quote_head"])
        else:
            head.append("quote", style=STYLES["quote_head"])
        title = node.find("div", "title")
        if topic and self.topic_id and topic != str(self.topic_id) and title is not None:
            a = title.find("a")
            if a is not None and a.text().strip():
                head.append(" in ", style=STYLES["muted"])
                head.append(_WS.sub(" ", a.text()).strip(), style="italic")
        if post_no:
            head.append(f" #{post_no}", style=STYLES["muted"])
        bq = node.find("blockquote")
        body = self.blocks(bq) if bq is not None else []
        return Prefixed(_stack([head, *body], gap=0), _bar(STYLES["quote_bar"], "┃"))

    def _onebox(self, node: Node) -> RenderableType:
        src = node.get("data-onebox-src")
        source = node.find("header", "source")
        domain = _WS.sub(" ", source.text()).strip() if source else ""
        if not domain and src:
            domain = re.sub(r"^https?://(www\.)?", "", src).split("/")[0]
        title_n = None
        for tag in ("h3", "h4", "h2"):
            title_n = node.find(tag)
            if title_n is not None:
                break
        title = _WS.sub(" ", title_n.text()).strip() if title_n is not None else ""
        a = title_n.find("a") if title_n is not None else None
        href = self._abs(a.get("href")) if a is not None else (src or "")
        desc_n = node.find(cls="github-repo-description") or node.find("p")
        desc = _WS.sub(" ", desc_n.text()).strip() if desc_n is not None else ""
        if len(desc) > 220:
            desc = desc[:219] + "…"
        lines: list[RenderableType] = []
        if domain:
            lines.append(Text(domain, style=STYLES["muted"]))
        if title:
            lines.append(Text(title, style=Style(bold=True) + (self._link_style(href) if href else Style())))
        if desc:
            lines.append(Text(desc, style=STYLES["muted"]))
        if not lines:
            lines.append(Text(href or "link", style=self._link_style(href) if href else ""))
        if href:
            self._add_link(title or domain or href, href)
        return Prefixed(Group(*lines), _bar(STYLES["onebox_bar"]))

    def _list(self, node: Node) -> RenderableType:
        ordered = node.tag == "ol"
        try:
            n = int(node.get("start") or 1)
        except ValueError:
            n = 1
        items: list[RenderableType] = []
        for li in node.children:
            if not isinstance(li, Node) or li.tag != "li":
                continue
            marker = f"{n}. " if ordered else "• "
            n += 1
            body = _stack(self.blocks(li), gap=0)
            items.append(Prefixed(body, Text(marker, style=STYLES["muted"]), " " * len(marker)))
        return Group(*items) if items else Text("")

    def _code(self, node: Node) -> RenderableType:
        code_n = node.find("code") or node
        code = code_n.text().rstrip("\n")
        self.out.code_blocks.append(code)
        lang = ""
        for c in code_n.classes:
            if c.startswith("lang-"):
                lang = c[5:]
        if lang in ("", "auto", "plaintext", "text", "nohighlight"):
            lang = _guess_lang(code) if lang == "auto" else "text"
        try:
            return Syntax(
                code,
                lang or "text",
                theme="monokai",
                word_wrap=True,
                background_color="#1e1e1e",
                padding=(0, 1),
            )
        except Exception:  # noqa: BLE001
            return Text(code, style=STYLES["code"])

    def _table(self, node: Node) -> RenderableType:
        rows: list[list[Text]] = []
        header: list[Text] = []
        for tr in node.find_all("tr"):
            cells: list[Text] = []
            is_head = False
            for cell in tr.children:
                if not isinstance(cell, Node) or cell.tag not in ("td", "th"):
                    continue
                is_head = is_head or cell.tag == "th"
                buf = Text()
                for c in cell.children:
                    self.inline(c, buf)
                cells.append(_strip_text(buf))
            if is_head and not header and not rows:
                header = cells
            elif cells:
                rows.append(cells)
        t = Table(box=box.SIMPLE_HEAD, show_header=bool(header), pad_edge=False, header_style="bold")
        cols = max([len(header)] + [len(r) for r in rows] + [1])
        for i in range(cols):
            t.add_column(header[i] if i < len(header) else "", overflow="fold")
        for r in rows:
            t.add_row(*(r + [Text("")] * (cols - len(r))))
        return t

    def _details(self, node: Node) -> list[RenderableType]:
        self.out.has_details = True
        summary = node.find("summary")
        label = _WS.sub(" ", summary.text()).strip() if summary else "details"
        if not self.expand:
            return [Text(f"▸ {label}", style="bold")]
        body: list[RenderableType] = []
        for c in node.children:
            if isinstance(c, Node) and c.tag == "summary":
                continue
            if isinstance(c, Node) and c.tag in BLOCK:
                body.extend(self.block(c))
            elif isinstance(c, str) and c.strip():
                body.append(Text(_WS.sub(" ", c).strip()))
        head = Text(f"▾ {label}", style="bold")
        return [head, Prefixed(_stack(body), "  ")]

    def _poll(self, node: Node) -> list[RenderableType]:
        name = node.get("data-poll-name") or "poll"
        polls = {p.get("name"): p for p in self.post.get("polls") or []}
        data = polls.get(name)
        mine = set((self.post.get("polls_votes") or {}).get(name) or [])
        lines: list[RenderableType] = []
        title_n = node.find(cls="poll-title")
        if data is None:
            # No structured data; fall back to the option list in the HTML.
            for li in node.find_all("li"):
                lines.append(Text("○ " + _WS.sub(" ", li.text()).strip()))
            return [Prefixed(Group(*lines) if lines else Text("poll"), _bar(STYLES["poll_bar"]))]
        status = data.get("status") or "open"
        head = Text("📊 ", style="bold")
        head.append(_WS.sub(" ", title_n.text()).strip() if title_n else "Poll", style="bold")
        meta = f"  {data.get('voters', 0)} voters"
        if status == "closed":
            meta += " · closed"
        head.append(meta, style=STYLES["muted"])
        lines.append(head)
        options = data.get("options") or []
        total = sum(int(o.get("votes") or 0) for o in options) or 0
        show = any("votes" in o for o in options)
        width = 18
        for o in options:
            label = _WS.sub(" ", parse(o.get("html") or "").text()).strip()
            mark = "●" if o.get("id") in mine else "○"
            line = Text(f"{mark} ", style="bold" if mark == "●" else "")
            line.append(label)
            if show:
                votes = int(o.get("votes") or 0)
                pct = round(100 * votes / total) if total else 0
                filled = round(width * pct / 100)
                line.append("\n  ")
                line.append("█" * filled, style=STYLES["poll_bar"])
                line.append("░" * (width - filled), style=STYLES["muted"])
                line.append(f" {pct}% ({votes})", style=STYLES["muted"])
            lines.append(line)
        return [Prefixed(Group(*lines), _bar(STYLES["poll_bar"]))]


def _guess_lang(code: str) -> str:
    if code.count("\n") < 1:
        return "text"
    try:
        from pygments.lexers import guess_lexer

        lexer = guess_lexer(code)
    except Exception:  # noqa: BLE001
        return "text"
    aliases = set(getattr(lexer, "aliases", []) or [])
    hit = aliases & _GUESSABLE
    return next(iter(hit)) if hit else "text"


def render_cooked(
    cooked: str,
    site: "Site | None",
    base_url: str,
    *,
    post: dict | None = None,
    topic_id: int | None = None,
    reveal_spoilers: bool = False,
    expand_details: bool = False,
) -> Rendered:
    r = Renderer(
        site,
        base_url,
        post=post,
        topic_id=topic_id,
        reveal_spoilers=reveal_spoilers,
        expand_details=expand_details,
    )
    root = parse(cooked)
    r.out.body = _stack(r.blocks(root))
    return r.out


def plain_text(cooked: str) -> str:
    """Rough plain text of cooked HTML, for excerpts and searching."""
    return _WS.sub(" ", parse(cooked).text()).strip()


def iter_lines(renderable: RenderableType, console: Console, width: int) -> Iterable[list[Segment]]:
    return console.render_lines(renderable, console.options.update(width=width, height=None), pad=False)
