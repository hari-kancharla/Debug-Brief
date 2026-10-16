"""Check all local HTML links, fragments and assets without network access."""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Set, Tuple
from urllib.parse import unquote, urlsplit


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: Set[str] = set()
        self.duplicates: List[str] = []
        self.links: List[str] = []
        self.references: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str]]) -> None:
        values = dict(attrs)
        anchor = values.get("id")
        if anchor:
            if anchor in self.ids:
                self.duplicates.append(anchor)
            self.ids.add(anchor)
        for name in ("href", "src"):
            if values.get(name):
                self.links.append(values[name])
        if values.get("aria-controls"):
            self.references.extend(values["aria-controls"].split())
        if values.get("aria-labelledby"):
            self.references.extend(values["aria-labelledby"].split())
        if tag == "meta" and values.get("property") == "og:image":
            self.links.append(values.get("content", ""))


def check_site(root: Path) -> List[str]:
    root = root.resolve()
    pages: Dict[Path, Page] = {}
    errors: List[str] = []
    for path in sorted(root.rglob("*.html")):
        page = Page()
        page.feed(path.read_text(encoding="utf-8"))
        pages[path] = page
        for duplicate in page.duplicates:
            errors.append(f"{path.name}: duplicate id #{duplicate}")
        for reference in page.references:
            if reference not in page.ids:
                errors.append(f"{path.name}: missing ARIA reference #{reference}")
    for path, page in pages.items():
        for link in page.links:
            url = urlsplit(link)
            if url.scheme or url.netloc:
                continue
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            if target.is_dir():
                target /= "index.html"
            if root not in target.parents:
                errors.append(f"{path.name}: path escapes site root: {link}")
            elif not target.is_file():
                errors.append(f"{path.name}: missing file: {link}")
            elif url.fragment and target.suffix == ".html":
                destination = pages.get(target)
                if destination is None or unquote(url.fragment) not in destination.ids:
                    errors.append(f"{path.name}: missing fragment: {link}")
    for path in root.rglob("*.css"):
        for link in re.findall(r"url\([\'\"]?([^\)\'\"]+)", path.read_text()):
            if not urlsplit(link).scheme and not (path.parent / link).is_file():
                errors.append(f"{path.name}: missing CSS asset: {link}")
    return errors


if __name__ == "__main__":
    failures = check_site(Path(__file__).resolve().parents[1] / "site")
    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        sys.exit(1)
    print("All local page links, fragments, ARIA references, and assets resolve.")
