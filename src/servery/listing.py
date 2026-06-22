"""Rich, sortable, searchable HTML directory listings.

Where ``http.server`` emits a bare ``<ul>`` of names, servery renders a table
with human-readable sizes and modification times, directories first. Columns are
sortable via the Apache ``mod_autoindex`` query convention (``?C=N|S|M&O=A|D``),
a ``?q=`` substring filter narrows the list, and a ``?ext=`` facet filters by
file type — all server-side, no JavaScript.

On top of the table the page renders a clickable breadcrumb, per-type icons,
relative timestamps (exact time on hover), inline size bars, an aggregate
metrics strip, and a pure-SVG modification timeline. Long directories are
paginated. A cookie-backed ``?theme=`` link switches light/dark/auto.

The markup is self-contained (inline CSS, light/dark aware, no scripts) and
escapes every user-controlled value, so it is safe under the strict
Content-Security-Policy servery applies to its own generated pages.
"""

from __future__ import annotations

import dataclasses
import functools
import html
import mimetypes
import os
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from servery import _log

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")

# Bound the per-request scan so a directory with millions of entries can't OOM/peg
# the server (the full list is sorted, faceted, and metric'd before pagination).
_MAX_SCAN_ENTRIES: int = 100_000

# Canonical sort name -> Apache column code, and the reverse.
_SORT_TO_CODE = {"name": "N", "size": "S", "date": "M"}
_CODE_TO_SORT = {code: name for name, code in _SORT_TO_CODE.items()}

# Rows per page before pagination kicks in. 0 disables it (used by callers that
# want the whole listing at once, e.g. the tests).
DEFAULT_PAGE_SIZE = 1000

# How many extension facet chips to show, most-common first.
_MAX_FACETS = 12


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
    # Local time, human display only. time.localtime + manual formatting avoids
    # both strftime's format-string parsing and a datetime allocation per entry.
    tm = time.localtime(ts)
    return f"{tm.tm_year:04d}-{tm.tm_mon:02d}-{tm.tm_mday:02d} {tm.tm_hour:02d}:{tm.tm_min:02d}"


def _relative_time(ts: float, now: float) -> str:
    """A compact human delta like "3h ago" or "just now"."""
    delta = now - ts
    if delta < 0:
        delta = 0.0
    if delta < 45:
        return "just now"
    if delta < 5400:  # < 90 min
        return f"{round(delta / 60)}m ago"
    if delta < 129600:  # < 36 h
        return f"{round(delta / 3600)}h ago"
    if delta < 1209600:  # < 14 d
        return f"{round(delta / 86400)}d ago"
    if delta < 7776000:  # < 90 d
        return f"{round(delta / 604800)}w ago"
    if delta < 31536000:  # < 1 y
        return f"{round(delta / 2592000)}mo ago"
    return f"{round(delta / 31536000)}y ago"


@functools.lru_cache(maxsize=4096)
def _extension(name: str) -> str:
    """Lowercased file extension without the dot; "" for none or a dotfile.

    Cached: it is called for every entry by both the row renderer (via
    :func:`_category`) and the facet counter, and filenames repeat across renders.
    """
    base = name.rsplit("/", 1)[-1]
    head, dot, ext = base.rpartition(".")
    # No dot, or a leading-dot dotfile with no further extension (".bashrc").
    if not dot or not head:
        return ""
    return ext.lower()


# Extension -> coarse category, for icons and the facet chips.
_EXT_CATEGORY = {
    # archives
    "zip": "archive",
    "tar": "archive",
    "gz": "archive",
    "tgz": "archive",
    "bz2": "archive",
    "xz": "archive",
    "7z": "archive",
    "rar": "archive",
    "zst": "archive",
    # images
    "png": "image",
    "jpg": "image",
    "jpeg": "image",
    "gif": "image",
    "webp": "image",
    "svg": "image",
    "bmp": "image",
    "ico": "image",
    "avif": "image",
    "heic": "image",
    "tiff": "image",
    # audio
    "mp3": "audio",
    "wav": "audio",
    "flac": "audio",
    "ogg": "audio",
    "m4a": "audio",
    "aac": "audio",
    # video
    "mp4": "video",
    "mkv": "video",
    "mov": "video",
    "webm": "video",
    "avi": "video",
    "m4v": "video",
    # documents
    "pdf": "pdf",
    "doc": "doc",
    "docx": "doc",
    "odt": "doc",
    "rtf": "doc",
    "ppt": "doc",
    "pptx": "doc",
    "xls": "sheet",
    "xlsx": "sheet",
    "ods": "sheet",
    "csv": "sheet",
    "tsv": "sheet",
    # code
    "py": "code",
    "js": "code",
    "ts": "code",
    "tsx": "code",
    "jsx": "code",
    "c": "code",
    "h": "code",
    "cpp": "code",
    "cc": "code",
    "hpp": "code",
    "rs": "code",
    "go": "code",
    "java": "code",
    "rb": "code",
    "php": "code",
    "sh": "code",
    "bash": "code",
    "zsh": "code",
    "pl": "code",
    "lua": "code",
    "sql": "code",
    "html": "code",
    "css": "code",
    "json": "code",
    "yaml": "code",
    "yml": "code",
    "toml": "code",
    "xml": "code",
    "ini": "code",
    "cfg": "code",
    # text
    "txt": "text",
    "md": "text",
    "rst": "text",
    "log": "text",
}

_CATEGORY_ICON = {
    "dir": "\N{FILE FOLDER}",
    "archive": "\N{PACKAGE}",
    "image": "\N{FRAME WITH PICTURE}",
    "audio": "\N{MULTIPLE MUSICAL NOTES}",
    "video": "\N{FILM FRAMES}",
    "pdf": "\N{CLOSED BOOK}",
    "doc": "\N{MEMO}",
    "sheet": "\N{BAR CHART}",
    "code": "\N{SCROLL}",
    "text": "\N{PAGE FACING UP}",
    "binary": "\N{PAGE WITH CURL}",
}


# mimetypes top-level -> category, for the long-tail extensions the hand-curated
# table doesn't list. Pure extension lookups, no file content is ever read.
_MIME_TOPLEVEL_CATEGORY = {"image": "image", "audio": "audio", "video": "video", "text": "text"}

# application/* subtypes worth a specific icon, matched as a substring so the
# verbose OOXML / vendor types ("…wordprocessingml.document") resolve too.
_MIME_APP_CATEGORY = (
    ("pdf", "pdf"),
    ("zip", "archive"),
    ("tar", "archive"),
    ("gzip", "archive"),
    ("compress", "archive"),
    ("x-7z", "archive"),
    ("x-rar", "archive"),
    ("x-bzip", "archive"),
    ("word", "doc"),
    ("opendocument.text", "doc"),
    ("powerpoint", "doc"),
    ("presentation", "doc"),
    ("excel", "sheet"),
    ("spreadsheet", "sheet"),
    ("json", "code"),
    ("xml", "code"),
    ("javascript", "code"),
    ("x-sh", "code"),
)


@functools.lru_cache(maxsize=4096)
def _ext_to_category(ext: str) -> str:
    """Map a file extension to a coarse icon/facet category.

    The hand-curated table wins; for the long tail we fall back to the stdlib
    ``mimetypes`` extension database. Both are pure string lookups — never any
    file content is read — and the (small) result is cached per extension.
    """
    if not ext:
        return "binary"
    known = _EXT_CATEGORY.get(ext)
    if known:
        return known
    guessed, _ = mimetypes.guess_type("_." + ext)
    if not guessed:
        return "binary"
    top, _, sub = guessed.partition("/")
    mapped = _MIME_TOPLEVEL_CATEGORY.get(top)
    if mapped:
        return mapped
    if top == "application":
        for needle, category in _MIME_APP_CATEGORY:
            if needle in sub:
                return category
    return "binary"


def _category(entry: EntryInfo) -> str:
    if entry.is_dir:
        return "dir"
    return _ext_to_category(_extension(entry.name))


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
            if len(entries) >= _MAX_SCAN_ENTRIES:  # stop before unbounded RAM/CPU
                _log.logger.warning(
                    "directory listing truncated at %d entries: %s", _MAX_SCAN_ENTRIES, fs_dir
                )
                break
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


def _filter(entries: list[EntryInfo], query: str, ext: str) -> list[EntryInfo]:
    if query:
        needle = query.lower()
        entries = [e for e in entries if needle in e.name.lower()]
    if ext:
        # Directories are kept so navigation still works while a type filter is on.
        wanted = ext.lower()
        entries = [e for e in entries if e.is_dir or _extension(e.name) == wanted]
    return entries


# --- small HTML fragment helpers -----------------------------------------


def _state_params(sort: str, order: str, query: str, ext: str, page: int) -> dict[str, str]:
    """The current non-default listing state, as URL params to preserve in links."""
    params: dict[str, str] = {}
    code = _SORT_TO_CODE.get(sort, "N")
    if code != "N":
        params["C"] = code
    if order == "desc":
        params["O"] = "D"
    if query:
        params["q"] = query
    if ext:
        params["ext"] = ext
    if page > 1:
        params["page"] = str(page)
    return params


def _href(base: dict[str, str], **override: str | None) -> str:
    """Build a query-string href from ``base`` with ``override`` applied.

    A ``None`` override drops the key; ``urlencode`` percent-encodes every value,
    so the result is safe to embed in an attribute after a single ``html.escape``.
    """
    params = dict(base)
    for key, value in override.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    qs = urllib.parse.urlencode(params)
    return html.escape("?" + qs, quote=True) if qs else "./"


def _sort_link(label: str, code: str, sort: str, order: str, base: dict[str, str]) -> str:
    active = _SORT_TO_CODE.get(sort) == code
    if active:
        new_order = "D" if order == "asc" else "A"
        arrow = (
            " \N{BLACK UP-POINTING TRIANGLE}"
            if order == "asc"
            else " \N{BLACK DOWN-POINTING TRIANGLE}"
        )
    else:
        new_order = "A"
        arrow = ""
    # Changing the sort resets pagination to the first page.
    href = _href(base, C=code, O=new_order, page=None)
    return f'<a href="{href}">{html.escape(label)}{arrow}</a>'


def _aria_sort(code: str, sort: str, order: str) -> str:
    if _SORT_TO_CODE.get(sort) != code:
        return ""
    return ' aria-sort="ascending"' if order == "asc" else ' aria-sort="descending"'


def _breadcrumb(display_path: str) -> str:
    """A clickable trail of absolute directory links ending in the current dir."""
    segments = [s for s in display_path.split("/") if s]
    crumbs = [f'<a href="/" title="Root">{html.escape("\N{HOUSE BUILDING}")}</a>']
    accum = ""
    for i, seg in enumerate(segments):
        accum += "/" + urllib.parse.quote(seg, errors="surrogatepass")
        label = html.escape(seg)
        if i == len(segments) - 1:
            crumbs.append(f'<span class="here">{label}</span>')
        else:
            href = html.escape(accum + "/", quote=True)
            crumbs.append(f'<a href="{href}">{label}</a>')
    sep = ' <span class="sep">\N{RIGHTWARDS ARROW}</span> '
    return sep.join(crumbs)


def _theme_links(theme: str, base: dict[str, str]) -> str:
    choices = (("auto", "Auto"), ("light", "Light"), ("dark", "Dark"))
    out = []
    for value, label in choices:
        href = _href(base, theme=value)
        cls = ' class="active"' if value == theme else ""
        out.append(f'<a{cls} href="{href}">{label}</a>')
    return '<nav class="theme" aria-label="Color theme">' + "".join(out) + "</nav>"


def _facets(entries: list[EntryInfo], ext: str, base: dict[str, str]) -> str:
    """Clickable extension filter chips, most common first."""
    counts: dict[str, int] = {}
    for e in entries:
        if e.is_dir:
            continue
        key = _extension(e.name)
        if key:
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return ""
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_MAX_FACETS]
    chips = []
    all_cls = ' class="active"' if not ext else ""
    chips.append(f'<a{all_cls} href="{_href(base, ext=None, page=None)}">All</a>')
    for name, count in ranked:
        cls = ' class="active"' if name == ext.lower() else ""
        href = _href(base, ext=name, page=None)
        chips.append(f'<a{cls} href="{href}">.{html.escape(name)} <b>{count}</b></a>')
    return '<nav class="facets" aria-label="Filter by type">' + "".join(chips) + "</nav>"


def _metrics(entries: list[EntryInfo], now: float) -> str:
    files = [e for e in entries if not e.is_dir]
    dirs = sum(1 for e in entries if e.is_dir)
    total_size = sum(e.size or 0 for e in files)
    mtimes = [e.mtime for e in entries if e.mtime is not None]
    largest = max((e.size or 0 for e in files), default=0)

    items = [
        f"<span><b>{len(files)}</b> file(s)</span>",
        f"<span><b>{dirs}</b> dir(s)</span>",
        f"<span><b>{_human_size(total_size)}</b> total</span>",
    ]
    if largest:
        # Aggregates only — never a filename, so the strip can't reorder relative
        # to the table rows (the listing's directories-first contract).
        items.append(f"<span>largest <b>{_human_size(largest)}</b></span>")
    if mtimes:
        newest = max(mtimes)
        items.append(
            f'<span title="{_format_mtime(newest)}">newest '
            f"<b>{_relative_time(newest, now)}</b></span>"
        )
    return '<div class="metrics">' + " ".join(items) + "</div>"


def _timeline_svg(entries: list[EntryInfo]) -> str:
    """A pure-SVG histogram of entry modification times (no JS, CSP-safe)."""
    mtimes = sorted(e.mtime for e in entries if e.mtime is not None)
    if len(mtimes) < 2:
        return ""
    lo, hi = mtimes[0], mtimes[-1]
    span = hi - lo
    if span <= 0:
        return ""

    buckets = 32
    counts = [0] * buckets
    for ts in mtimes:
        idx = int((ts - lo) / span * buckets)
        if idx >= buckets:
            idx = buckets - 1
        counts[idx] += 1
    peak = max(counts)

    view_w, view_h = 320.0, 40.0
    bar_w = view_w / buckets
    rects = []
    for i, count in enumerate(counts):
        if not count:
            continue
        height = (count / peak) * (view_h - 2)
        x = i * bar_w
        y = view_h - height
        start = _format_mtime(lo + (span * i / buckets))
        rects.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w - 0.6:.2f}" height="{height:.2f}">'
            f"<title>{count} item(s) · around {start}</title></rect>"
        )
    return (
        '<figure class="timeline">'
        f'<svg viewBox="0 0 {view_w:.0f} {view_h:.0f}" preserveAspectRatio="none" '
        'role="img" aria-label="Modification activity over time">' + "".join(rects) + "</svg>"
        f"<figcaption>Modified ({_format_mtime(lo)}) "
        f"\N{RIGHTWARDS ARROW} ({_format_mtime(hi)})</figcaption>"
        "</figure>"
    )


def _pager(page: int, total_pages: int, total: int, per_page: int, base: dict[str, str]) -> str:
    if total_pages <= 1:
        return ""
    first = (page - 1) * per_page + 1
    last = min(page * per_page, total)

    def link(target: int, label: str, *, enabled: bool) -> str:
        if not enabled:
            return f'<span class="disabled">{label}</span>'
        ref = _href(base, page=None if target == 1 else str(target))
        return f'<a href="{ref}">{label}</a>'

    prev = link(page - 1, "\N{LEFTWARDS ARROW} Prev", enabled=page > 1)
    nxt = link(page + 1, "Next \N{RIGHTWARDS ARROW}", enabled=page < total_pages)
    return (
        '<nav class="pager" aria-label="Pagination">'
        f"{prev}"
        f'<span class="range">{first}\N{EN DASH}{last} of {total}</span>'
        f"{nxt}"
        "</nav>"
    )


def _row(entry: EntryInfo, max_size: int, now: float) -> str:
    suffix = "/" if entry.is_dir else ""
    display = entry.name + suffix
    # quote() percent-encodes every HTML-special character (< > & " '), so the
    # resulting href is already safe to drop into the attribute — escaping it
    # again is a no-op (and html.escape was a hot spot in the listing path).
    # quote(name + "/") == quote(name) + "/" ("/" is a safe char), so quote once.
    quoted = urllib.parse.quote(entry.name, errors="surrogatepass")
    icon = _CATEGORY_ICON[_category(entry)]
    name_cell = f'<span class="icon" aria-hidden="true">{icon}</span>'
    name_cell += f'<a href="{quoted + suffix}">{html.escape(display)}</a>'
    if entry.is_symlink:
        name_cell += ' <span class="sym">\N{RIGHTWARDS ARROW}</span>'
    if not entry.is_dir:
        dl = quoted + "?download=1"
        name_cell += f'<a class="dl" href="{dl}" download title="Download">\N{DOWNWARDS ARROW}</a>'

    if entry.size is None:
        size_cell = "\N{EM DASH}"
    else:
        pct = max(2, round(entry.size / max_size * 100)) if (max_size and entry.size) else 0
        bar = f'<span class="bar" style="width:{pct}%" aria-hidden="true"></span>' if pct else ""
        size_cell = f'<span class="num">{_human_size(entry.size)}</span>{bar}'

    if entry.mtime is None:
        mtime_cell = ""
    else:
        mtime_cell = (
            f'<span title="{_format_mtime(entry.mtime)}">{_relative_time(entry.mtime, now)}</span>'
        )

    return (
        f'<tr><td class="name">{name_cell}</td>'
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
    ext: str = "",
    page: int = 1,
    per_page: int = 0,
    theme: str = "auto",
    upload: bool = False,
) -> bytes:
    """Render a directory listing page as UTF-8 bytes.

    ``fs_dir`` is the filesystem directory; ``display_path`` is the decoded URL
    path. ``sort`` is one of ``name``/``size``/``date``, ``order`` is
    ``asc``/``desc``, ``query`` is a case-insensitive name filter, and ``ext``
    restricts to a single file extension. ``page``/``per_page`` paginate the
    rows (``per_page=0`` shows everything). ``theme`` is ``auto``/``light``/
    ``dark``. Raises ``OSError`` if the directory cannot be scanned.
    """
    now = time.time()
    scanned = _scan(fs_dir, show_hidden=show_hidden)
    filtered = _filter(scanned, query, ext)
    entries = _sorted(filtered, sort, order)
    total = len(entries)
    max_size = max((e.size or 0 for e in entries if not e.is_dir), default=0)

    if per_page and per_page > 0:
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(max(page, 1), total_pages)
        visible = entries[(page - 1) * per_page : page * per_page]
    else:
        total_pages = 1
        page = 1
        visible = entries

    base = _state_params(sort, order, query, ext, page)

    rows: list[str] = []
    if display_path != "/" and page == 1:
        rows.append(
            '<tr><td class="name"><span class="icon" aria-hidden="true">'
            '\N{UPWARDS ARROW}</span><a href="../">../</a></td>'
            '<td class="size">\N{EM DASH}</td><td></td></tr>'
        )
    rows.extend(_row(e, max_size, now) for e in visible)

    if not visible:
        if query or ext:
            clear = _href(base, q=None, ext=None, page=None)
            empty = (
                '<tr><td class="empty" colspan="3">No items match the current filter. '
                f'<a href="{clear}">Clear filters</a></td></tr>'
            )
        else:
            empty = '<tr><td class="empty" colspan="3">This directory is empty.</td></tr>'
        rows.append(empty)

    document = _TEMPLATE.format(
        style=_CSS,
        data_theme=html.escape(theme, quote=True),
        breadcrumb=_breadcrumb(display_path),
        theme_links=_theme_links(theme, base),
        heading=html.escape(display_path),
        upload_form=_UPLOAD_FORM if upload else "",
        search_value=html.escape(query, quote=True),
        facets=_facets(filtered, ext, base),
        metrics=_metrics(filtered, now),
        timeline=_timeline_svg(filtered),
        name_header=_sort_link("Name", "N", sort, order, base),
        size_header=_sort_link("Size", "S", sort, order, base),
        mtime_header=_sort_link("Modified", "M", sort, order, base),
        name_aria=_aria_sort("N", sort, order),
        size_aria=_aria_sort("S", sort, order),
        mtime_aria=_aria_sort("M", sort, order),
        rows="\n".join(rows),
        pager=_pager(page, total_pages, total, per_page, base),
        count=total,
    )
    return document.encode("utf-8", "surrogateescape")


_CSS = """
:root { color-scheme: light dark; --accent: #2563eb; }
html[data-theme="light"] { color-scheme: light; }
html[data-theme="dark"] { color-scheme: dark; }
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 64rem; padding: 0 1rem; }
.topbar { display: flex; align-items: center; justify-content: space-between;
  gap: 1rem; flex-wrap: wrap; }
h1.crumbs { font-size: 1.05rem; font-weight: 600; word-break: break-all; margin: 0; }
h1.crumbs a { color: var(--accent); }
h1.crumbs .sep, h1.crumbs .here { opacity: 0.6; font-weight: 400; }
nav.theme { display: flex; gap: 0; font-size: 0.78rem;
  border: 1px solid color-mix(in srgb, currentColor 25%, transparent); border-radius: 0.4rem;
  overflow: hidden; }
nav.theme a { padding: 0.25rem 0.6rem; color: inherit; opacity: 0.65; }
nav.theme a.active { opacity: 1; background: color-mix(in srgb, currentColor 12%, transparent); }
.search { margin: 0.75rem 0 0.5rem; }
.search input { padding: 0.4rem 0.6rem; width: 100%; max-width: 22rem; box-sizing: border-box;
  border: 1px solid color-mix(in srgb, currentColor 25%, transparent); border-radius: 0.4rem;
  background: Canvas; color: CanvasText; }
nav.facets { display: flex; flex-wrap: wrap; gap: 0.35rem; margin: 0.5rem 0; font-size: 0.78rem; }
nav.facets a { padding: 0.15rem 0.55rem; color: inherit; text-decoration: none;
  border: 1px solid color-mix(in srgb, currentColor 22%, transparent); border-radius: 1rem;
  opacity: 0.85; }
nav.facets a.active { background: var(--accent); border-color: var(--accent); color: white;
  opacity: 1; }
nav.facets a b { font-weight: 600; opacity: 0.7; }
nav.facets a.active b { opacity: 0.85; }
.metrics { display: flex; flex-wrap: wrap; gap: 0.4rem 1.1rem; margin: 0.6rem 0;
  font-size: 0.82rem; opacity: 0.85; }
.metrics b { font-weight: 600; }
figure.timeline { margin: 0.4rem 0 0.8rem; }
figure.timeline svg { width: 100%; height: 40px; display: block; }
figure.timeline rect { fill: var(--accent); opacity: 0.55; }
figure.timeline figcaption { font-size: 0.72rem; opacity: 0.6; margin-top: 0.2rem; }
table { border-collapse: collapse; width: 100%; }
thead th { position: sticky; top: 0; background: Canvas;
  box-shadow: 0 1px 0 color-mix(in srgb, currentColor 15%, transparent); }
th, td { text-align: left; padding: 0.3rem 0.6rem;
  border-bottom: 1px solid color-mix(in srgb, currentColor 12%, transparent); }
th { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; }
th a { color: inherit; opacity: 0.7; }
tbody tr:hover { background: color-mix(in srgb, currentColor 6%, transparent); }
td.name { width: 100%; overflow-wrap: anywhere; }
td.name .icon { display: inline-block; width: 1.4em; }
td.size, th.size, td.mtime, th.mtime { text-align: right; white-space: nowrap;
  font-variant-numeric: tabular-nums; }
td.size .num { display: block; }
td.size .bar { display: block; height: 3px; margin-top: 2px; margin-left: auto; border-radius: 2px;
  background: var(--accent); opacity: 0.35; min-width: 2px; }
td.empty { text-align: center; padding: 2rem 0.6rem; opacity: 0.7; }
a { text-decoration: none; }
a:hover { text-decoration: underline; }
a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 2px; }
a.dl { opacity: 0; margin-left: 0.5rem; font-size: 0.85em; }
tr:hover a.dl, a.dl:focus-visible { opacity: 0.7; }
.sym { opacity: 0.6; }
nav.pager { display: flex; align-items: center; justify-content: center; gap: 1rem;
  margin: 1rem 0; font-size: 0.85rem; }
nav.pager a { color: var(--accent); }
nav.pager .disabled { opacity: 0.35; }
nav.pager .range { opacity: 0.7; }
footer { margin-top: 1rem; font-size: 0.8rem; opacity: 0.6; }
/* Touch devices cannot hover: keep the per-file download affordance visible and
   give the chips / toggles / pager comfortable finger-sized tap targets. */
@media (hover: none) {
  a.dl { opacity: 0.7; }
  nav.theme a, nav.facets a { padding: 0.5rem 0.8rem; }
  nav.pager a, nav.pager .disabled { padding: 0.4rem 0.3rem; }
}
"""

_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en" data-theme="{data_theme}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Index of {heading}</title>
<style>{style}</style>
</head>
<body>
<div class="topbar">
<h1 class="crumbs">{breadcrumb}</h1>
{theme_links}
</div>
{upload_form}
<form class="search" method="get">
<input type="search" name="q" value="{search_value}" placeholder="Filter\N{HORIZONTAL ELLIPSIS}" \
aria-label="Filter">
</form>
{facets}
{metrics}
{timeline}
<table>
<thead><tr><th class="name"{name_aria}>{name_header}</th><th class="size"{size_aria}>{size_header}</th>\
<th class="mtime"{mtime_aria}>{mtime_header}</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
{pager}
<footer>{count} item(s) · download <a href="?archive=tar.gz">tar.gz</a> · \
<a href="?archive=zip">zip</a> · served by servery</footer>
</body>
</html>
"""
