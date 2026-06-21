"""Rich, sortable, searchable HTML directory listings.

Where ``http.server`` emits a bare ``<ul>`` of names, servery renders a table
with human-readable sizes and modification times, directories first. Columns are
sortable via the Apache ``mod_autoindex`` query convention (``?C=N|S|M&O=A|D``),
and a ``?q=`` substring filter narrows the list — both server-side, no JavaScript.

The markup is self-contained (inline CSS, light/dark aware) and escapes every
user-controlled value, so it is safe under a strict Content-Security-Policy.
"""

from __future__ import annotations

import dataclasses
import datetime
import html
import os
import urllib.parse
from collections.abc import Callable
from typing import Any

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")

# Canonical sort name -> Apache column code, and the reverse.
_SORT_TO_CODE = {"name": "N", "size": "S", "date": "M"}
_CODE_TO_SORT = {code: name for name, code in _SORT_TO_CODE.items()}


@dataclasses.dataclass(frozen=True, slots=True)
class EntryInfo:
    """A single directory entry, with stat data already resolved."""

    name: str
    is_dir: bool
    is_symlink: bool
    size: int | None
    mtime: float | None


def code_to_sort(code: str) -> str:
    """Map a URL column code (N/S/M) to a canonical sort name."""
    return _CODE_TO_SORT.get(code, "name")


def _human_size(num: int) -> str:
    value = float(num)
    for unit in _UNITS:
        if value < 1024.0 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{num} B"  # pragma: no cover - unreachable; satisfies the type checker


def _format_mtime(ts: float) -> str:
    # Local time, for human display only.
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _scan(fs_dir: str, *, show_hidden: bool) -> list[EntryInfo]:
    entries: list[EntryInfo] = []
    with os.scandir(fs_dir) as it:
        for entry in it:
            if not show_hidden and entry.name.startswith("."):
                continue
            # Match os.path.* behavior: swallow per-entry stat errors.
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            try:
                is_symlink = entry.is_symlink()
            except OSError:
                is_symlink = False
            size: int | None = None
            mtime: float | None = None
            try:
                stat = entry.stat()
                mtime = stat.st_mtime
                if not is_dir:
                    size = stat.st_size
            except OSError:
                pass
            entries.append(
                EntryInfo(
                    name=entry.name,
                    is_dir=is_dir,
                    is_symlink=is_symlink,
                    size=size,
                    mtime=mtime,
                )
            )
    return entries


def _key_func(sort: str) -> Callable[[EntryInfo], Any]:
    if sort == "size":
        return lambda e: e.size or 0
    if sort == "date":
        return lambda e: e.mtime or 0.0
    return lambda e: e.name.lower()


def _sorted(entries: list[EntryInfo], sort: str, order: str) -> list[EntryInfo]:
    result = sorted(entries, key=_key_func(sort), reverse=(order == "desc"))
    # Stable second pass keeps directories first regardless of the column/order.
    result.sort(key=lambda e: not e.is_dir)
    return result


def _filter(entries: list[EntryInfo], query: str) -> list[EntryInfo]:
    if not query:
        return entries
    needle = query.lower()
    return [e for e in entries if needle in e.name.lower()]


def _sort_link(label: str, code: str, sort: str, order: str, query: str) -> str:
    active = _SORT_TO_CODE.get(sort) == code
    if active:
        new_order = "D" if order == "asc" else "A"
        arrow = " ▲" if order == "asc" else " ▼"
    else:
        new_order = "A"
        arrow = ""
    params = {"C": code, "O": new_order}
    if query:
        params["q"] = query
    href = "?" + urllib.parse.urlencode(params)
    return f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}{arrow}</a>'


def _row(entry: EntryInfo) -> str:
    display = entry.name + "/" if entry.is_dir else entry.name
    href = urllib.parse.quote(entry.name + ("/" if entry.is_dir else ""), errors="surrogatepass")
    name_cell = html.escape(display)
    if entry.is_symlink:
        name_cell += ' <span class="sym">→</span>'
    size_cell = "—" if entry.size is None else _human_size(entry.size)
    mtime_cell = "" if entry.mtime is None else _format_mtime(entry.mtime)
    return (
        f'<tr><td class="name"><a href="{html.escape(href, quote=True)}">{name_cell}</a></td>'
        f'<td class="size">{size_cell}</td><td class="mtime">{mtime_cell}</td></tr>'
    )


_UPLOAD_FORM = (
    '<form class="upload" method="post" enctype="multipart/form-data">'
    '<input type="file" name="file" multiple required>'
    '<button type="submit">Upload</button></form>'
)


def render(
    fs_dir: str,
    display_path: str,
    *,
    show_hidden: bool,
    sort: str = "name",
    order: str = "asc",
    query: str = "",
    upload: bool = False,
) -> bytes:
    """Render a directory listing page as UTF-8 bytes.

    ``fs_dir`` is the filesystem directory; ``display_path`` is the decoded URL
    path. ``sort`` is one of ``name``/``size``/``date``, ``order`` is
    ``asc``/``desc``, and ``query`` is a case-insensitive name filter. Raises
    ``OSError`` if the directory cannot be scanned.
    """
    entries = _sorted(_filter(_scan(fs_dir, show_hidden=show_hidden), query), sort, order)

    rows: list[str] = []
    if display_path != "/":
        rows.append(
            '<tr><td class="name"><a href="../">../</a></td><td class="size">—</td><td></td></tr>'
        )
    rows.extend(_row(e) for e in entries)

    document = _TEMPLATE.format(
        heading=html.escape(display_path),
        upload_form=_UPLOAD_FORM if upload else "",
        search_value=html.escape(query, quote=True),
        name_header=_sort_link("Name", "N", sort, order, query),
        size_header=_sort_link("Size", "S", sort, order, query),
        mtime_header=_sort_link("Modified", "M", sort, order, query),
        rows="\n".join(rows),
        count=len(entries),
    )
    return document.encode("utf-8", "surrogateescape")


_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Index of {heading}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 60rem; padding: 0 1rem; }}
h1 {{ font-size: 1.2rem; font-weight: 600; word-break: break-all; }}
.search {{ margin: 0.5rem 0 1rem; }}
.search input {{ padding: 0.35rem 0.6rem; width: 100%; max-width: 20rem; box-sizing: border-box; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; padding: 0.3rem 0.6rem;
  border-bottom: 1px solid color-mix(in srgb, currentColor 15%, transparent); }}
th {{ font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; }}
th a {{ color: inherit; opacity: 0.7; }}
td.size, th.size, td.mtime, th.mtime {{ text-align: right; white-space: nowrap;
  font-variant-numeric: tabular-nums; }}
a {{ text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.sym {{ opacity: 0.6; }}
footer {{ margin-top: 1rem; font-size: 0.8rem; opacity: 0.6; }}
</style>
</head>
<body>
<h1>Index of {heading}</h1>
{upload_form}
<form class="search" method="get">
<input type="search" name="q" value="{search_value}" placeholder="Filter…" aria-label="Filter">
</form>
<table>
<thead><tr><th class="name">{name_header}</th><th class="size">{size_header}</th>\
<th class="mtime">{mtime_header}</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
<footer>{count} item(s) · served by servery</footer>
</body>
</html>
"""
