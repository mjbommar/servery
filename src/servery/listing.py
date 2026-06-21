"""Rich HTML directory listings.

Where ``http.server`` emits a bare ``<ul>`` of names, servery renders a table
with human-readable sizes and modification times, directories first. The markup
is self-contained (inline CSS, light/dark aware), escapes every user-controlled
value, and carries no inline script — so it is safe to serve under a strict
Content-Security-Policy.

The sort here is fixed (directories first, then case-insensitive name); the
client-driven ``?C=&O=`` sort scheme arrives in v0.2 and will slot into
:func:`_sort_key`.
"""

from __future__ import annotations

import dataclasses
import datetime
import html
import os
import urllib.parse

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


@dataclasses.dataclass(frozen=True, slots=True)
class EntryInfo:
    """A single directory entry, with stat data already resolved."""

    name: str
    is_dir: bool
    is_symlink: bool
    size: int | None
    mtime: float | None


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


def _sort_key(entry: EntryInfo) -> tuple[bool, str]:
    # Directories first (False sorts before True), then case-insensitive name.
    return (not entry.is_dir, entry.name.lower())


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


def render(fs_dir: str, display_path: str, *, show_hidden: bool) -> bytes:
    """Render a directory listing page as UTF-8 bytes.

    ``fs_dir`` is the filesystem directory; ``display_path`` is the decoded URL
    path (used for the heading and the parent link). Raises ``OSError`` if the
    directory cannot be scanned.
    """
    entries = sorted(_scan(fs_dir, show_hidden=show_hidden), key=_sort_key)
    safe_heading = html.escape(display_path)

    rows: list[str] = []
    if display_path != "/":
        rows.append(
            '<tr><td class="name"><a href="../">../</a></td><td class="size">—</td><td></td></tr>'
        )
    rows.extend(_row(e) for e in entries)

    document = _TEMPLATE.format(
        heading=safe_heading,
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
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; padding: 0.3rem 0.6rem; border-bottom: 1px solid color-mix(in srgb, currentColor 15%, transparent); }}
th {{ font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; opacity: 0.7; }}
td.size, th.size, td.mtime, th.mtime {{ text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }}
a {{ text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.sym {{ opacity: 0.6; }}
footer {{ margin-top: 1rem; font-size: 0.8rem; opacity: 0.6; }}
</style>
</head>
<body>
<h1>Index of {heading}</h1>
<table>
<thead><tr><th class="name">Name</th><th class="size">Size</th><th class="mtime">Modified</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
<footer>{count} item(s) · served by servery</footer>
</body>
</html>
"""
