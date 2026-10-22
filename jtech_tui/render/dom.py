from __future__ import annotations

from html.parser import HTMLParser

VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "source", "track", "wbr",
}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None, parent: "Node | None" = None) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node | str] = []
        self.parent = parent

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def has_class(self, name: str) -> bool:
        return name in self.classes

    def get(self, key: str, default: str = "") -> str:
        return self.attrs.get(key) or default

    def text(self) -> str:
        out: list[str] = []
        for c in self.children:
            out.append(c if isinstance(c, str) else c.text())
        return "".join(out)

    def find(self, tag: str | None = None, cls: str | None = None) -> "Node | None":
        for c in self.children:
            if isinstance(c, str):
                continue
            if (tag is None or c.tag == tag) and (cls is None or c.has_class(cls)):
                return c
            hit = c.find(tag, cls)
            if hit is not None:
                return hit
        return None

    def find_all(self, tag: str | None = None, cls: str | None = None) -> list["Node"]:
        out: list[Node] = []
        for c in self.children:
            if isinstance(c, str):
                continue
            if (tag is None or c.tag == tag) and (cls is None or c.has_class(cls)):
                out.append(c)
            out.extend(c.find_all(tag, cls))
        return out

    def __repr__(self) -> str:
        return f"<{self.tag} {self.attrs.get('class', '')}>"


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.cur = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: v or "" for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.cur.children.append(Node(tag, {k: v or "" for k, v in attrs}, self.cur))

    def handle_endtag(self, tag: str) -> None:
        # Walk up to the matching open tag; tolerate stray closers.
        n: Node | None = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data: str) -> None:
        if data:
            self.cur.children.append(data)


def parse(html: str) -> Node:
    b = _Builder()
    try:
        b.feed(html or "")
        b.close()
    except Exception:  # noqa: BLE001
        pass
    return b.root
