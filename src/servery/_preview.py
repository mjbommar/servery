"""The opt-in ``--preview`` page: look inside a file without downloading it.

``GET /notes.md?preview=1`` renders a self-contained HTML page around the file's
content instead of sending the bytes. What "render" means depends on the type:

============ ============================================================
Markdown     rendered to HTML (:mod:`servery._markdown`), or highlighted source
Source code  syntax-highlighted with line numbers (:mod:`servery._highlight`)
Notebooks    markdown cells rendered, code cells highlighted, text outputs shown
JSON         re-indented and highlighted
CSV / TSV    a real table (bounded), or highlighted source
Images       shown inline; audio/video get native controls
Everything   plain escaped text, or an honest "binary file" card
============ ============================================================

``?preview=source`` forces the highlighted-source view for anything textual, so
a Markdown file can be read either way.

The page is script-free and every byte of file content is escaped or passed
through the Markdown renderer's fixed tag vocabulary, so it stays inside the
strict CSP servery puts on its own generated pages (:data:`CSP` widens the
listing's policy only by ``media-src 'self'``, for the audio/video players).
Nothing here is reachable unless the operator passed ``--preview``.
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import urllib.parse

from servery import _highlight, _markdown, _metadata, listing

# Largest file the preview will read and render. Bigger files get a card with a
# download link instead — this page holds the whole thing in memory.
DEFAULT_MAX_BYTES = 2 * 1024 * 1024

# Bounds for the derived views, so one hostile file cannot produce a huge page.
_MAX_TABLE_ROWS = 500
_MAX_TABLE_COLUMNS = 60
_MAX_NOTEBOOK_CELLS = 300
_MAX_OUTPUT_CHARS = 4000

# The preview page's CSP: the listing's policy plus media-src, for <audio>/<video>.
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; media-src 'self'; "
    "form-action 'self'; frame-ancestors 'self'"
)

_MARKDOWN_EXTENSIONS = frozenset(("md", "markdown", "mdown", "mkd", "mkdn"))
_IMAGE_EXTENSIONS = frozenset(
    ("png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico", "avif", "apng")
)
_AUDIO_EXTENSIONS = frozenset(("mp3", "wav", "ogg", "oga", "flac", "m4a", "aac", "opus"))
_VIDEO_EXTENSIONS = frozenset(("mp4", "webm", "ogv", "mov", "m4v"))
_TABLE_EXTENSIONS = frozenset(("csv", "tsv"))
_TEXT_EXTENSIONS = frozenset(("txt", "text", "log", "rst", "org", "adoc", "asc", "nfo", "srt"))

# Kinds with a *rendered* view distinct from their source.
_RENDERABLE = frozenset(("markdown", "notebook", "json", "table"))


def _extension(name: str) -> str:
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    head, dot, ext = base.rpartition(".")
    return ext if dot and head else ""


def kind_for(name: str) -> str:
    """Coarse preview kind for ``name`` — "binary" when there is nothing to show."""
    ext = _extension(name)
    if ext in _MARKDOWN_EXTENSIONS:
        return "markdown"
    if ext == "ipynb":
        return "notebook"
    if ext in ("json", "jsonc", "geojson"):
        return "json"
    if ext in _TABLE_EXTENSIONS:
        return "table"
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in _AUDIO_EXTENSIONS:
        return "audio"
    if ext in _VIDEO_EXTENSIONS:
        return "video"
    if ext == "pdf":
        return "pdf"
    language = _highlight.language_for(name)
    if language is not None and language != "text":
        return "code"
    if language == "text" or ext in _TEXT_EXTENSIONS or not ext:
        return "text"
    return "binary"


def previewable(name: str) -> bool:
    """True when :func:`render` has something better than a download to offer."""
    return kind_for(name) != "binary"


def modes_for(kind: str) -> tuple[str, ...]:
    """The view modes offered for ``kind``, most useful first."""
    if kind in _RENDERABLE:
        return ("render", "source")
    if kind in ("image", "audio", "video", "pdf", "binary"):
        return ()
    return ("source",)


# --- content renderers ---------------------------------------------------


def _decode(data: bytes) -> str | None:
    """Decode file bytes as text, or ``None`` when they are clearly binary."""
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")


def _source_view(text: str, name: str) -> str:
    return _highlight.code_block(text, _highlight.language_for(name))


def _json_view(text: str) -> str:
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return _highlight.code_block(text, "json")
    pretty = json.dumps(parsed, indent=2, ensure_ascii=False, sort_keys=False)
    return _highlight.code_block(pretty, "json")


def _table_view(text: str, name: str) -> str:
    delimiter = "\t" if name.lower().endswith(".tsv") else ","
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error:
        return _highlight.code_block(text, None)
    rows = [row for row in rows if row]
    if not rows:
        return '<p class="empty">No rows.</p>'
    truncated = len(rows) > _MAX_TABLE_ROWS + 1
    header, body = rows[0][:_MAX_TABLE_COLUMNS], rows[1 : _MAX_TABLE_ROWS + 1]

    def row_html(cells: list[str], tag: str) -> str:
        trimmed = cells[:_MAX_TABLE_COLUMNS]
        return (
            "<tr>"
            + "".join(f"<{tag}>{html.escape(cell, quote=False)}</{tag}>" for cell in trimmed)
            + "</tr>"
        )

    table = (
        '<div class="tablewrap"><table class="data"><thead>'
        + row_html(header, "th")
        + "</thead><tbody>"
        + "".join(row_html(row, "td") for row in body)
        + "</tbody></table></div>"
    )
    if truncated:
        table += f'<p class="note">Showing the first {_MAX_TABLE_ROWS} rows of {len(rows) - 1}.</p>'
    return table


def _notebook_view(text: str) -> str:
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return _highlight.code_block(text, "json")
    cells = parsed.get("cells") if isinstance(parsed, dict) else None
    if not isinstance(cells, list):
        return _highlight.code_block(text, "json")
    language = "python"
    info = parsed.get("metadata", {})
    if isinstance(info, dict) and isinstance(info.get("language_info"), dict):
        language = str(info["language_info"].get("name") or "python")
    resolved = _highlight.language_for_info(language) or "python"

    parts: list[str] = []
    for cell in cells[:_MAX_NOTEBOOK_CELLS]:
        if not isinstance(cell, dict):
            continue
        source = cell.get("source")
        body = "".join(source) if isinstance(source, list) else str(source or "")
        if cell.get("cell_type") == "markdown":
            parts.append(f'<div class="md">{_markdown.render(body)}</div>')
        elif cell.get("cell_type") == "code":
            parts.append('<div class="cell">')
            parts.append(_highlight.code_block(body, resolved))
            outputs = cell.get("outputs")
            if isinstance(outputs, list):
                rendered = _notebook_outputs(outputs)
                if rendered:
                    parts.append(rendered)
            parts.append("</div>")
    if len(cells) > _MAX_NOTEBOOK_CELLS:
        parts.append(
            f'<p class="note">Showing the first {_MAX_NOTEBOOK_CELLS} of {len(cells)} cells.</p>'
        )
    return "".join(parts)


def _notebook_outputs(outputs: list[object]) -> str:
    collected: list[str] = []
    budget = _MAX_OUTPUT_CHARS
    for output in outputs:
        if not isinstance(output, dict) or budget <= 0:
            continue
        text = ""
        if "text" in output:
            raw = output["text"]
            text = "".join(raw) if isinstance(raw, list) else str(raw)
        elif isinstance(output.get("data"), dict):
            raw = output["data"].get("text/plain", "")
            text = "".join(raw) if isinstance(raw, list) else str(raw)
        elif output.get("output_type") == "error":
            raw = output.get("traceback", [])
            text = "\n".join(str(line) for line in raw) if isinstance(raw, list) else str(raw)
        if not text:
            continue
        collected.append(html.escape(text[:budget], quote=False))
        budget -= len(text)
    if not collected:
        return ""
    return '<pre class="out">' + "".join(collected) + "</pre>"


def _media_view(kind: str, quoted: str) -> str:
    if kind == "image":
        return f'<p class="media"><img src="{quoted}" alt="" loading="lazy"></p>'
    if kind == "audio":
        return f'<p class="media"><audio controls src="{quoted}"></audio></p>'
    return f'<p class="media"><video controls src="{quoted}"></video></p>'


def _card(message: str, quoted: str, *, label: str = "Download") -> str:
    return (
        f'<div class="card"><p>{message}</p>'
        f'<p><a class="button" href="{quoted}?download=1">{label}</a> '
        f'<a class="button ghost" href="{quoted}">Open raw</a></p></div>'
    )


# --- the page ------------------------------------------------------------


def _metadata_panel(meta: _metadata.Meta) -> str:
    if not meta:
        return ""
    rows: list[tuple[str, str]] = []
    for name in ("title", "author", "description", "date", "version", "lang"):
        value = getattr(meta, name)
        if value:
            rows.append((name, value))
    if meta.tags:
        rows.append(("tags", ", ".join(meta.tags)))
    rows.extend(meta.extra)
    if not rows:
        return ""
    items = "".join(
        f"<div><dt>{html.escape(name)}</dt><dd>{html.escape(value)}</dd></div>"
        for name, value in rows
    )
    return f'<details class="meta" open><summary>Metadata</summary><dl>{items}</dl></details>'


def _mode_links(kind: str, mode: str, quoted: str) -> str:
    modes = modes_for(kind)
    if len(modes) < 2:
        return ""
    labels = {"render": "Rendered", "source": "Source"}
    chips = []
    for value in modes:
        active = ' class="active"' if value == mode else ""
        chips.append(f'<a{active} href="{quoted}?preview={value}">{labels[value]}</a>')
    return '<nav class="modes" aria-label="View mode">' + "".join(chips) + "</nav>"


def render(
    fs_path: str,
    url_path: str,
    *,
    mode: str = "",
    max_bytes: int = DEFAULT_MAX_BYTES,
    theme: str = "auto",
    metadata: bool = False,
    metadata_max_bytes: int = _metadata.DEFAULT_MAX_BYTES,
) -> bytes:
    """Render the preview page for ``fs_path`` as UTF-8 bytes.

    ``url_path`` is the decoded request path (used for the breadcrumb and the
    links back to the raw file). ``mode`` is ``render``/``source``/``""``.
    Raises ``OSError`` if the file cannot be read.
    """
    name = os.path.basename(url_path.rstrip("/")) or os.path.basename(fs_path)
    quoted = urllib.parse.quote(name, errors="surrogatepass")
    kind = kind_for(name)
    modes = modes_for(kind)
    if mode not in modes:
        mode = modes[0] if modes else ""

    stat = os.stat(fs_path)
    meta = (
        _metadata.for_file(fs_path, max_bytes=metadata_max_bytes) if metadata else _metadata.EMPTY
    )

    if kind in ("image", "audio", "video"):
        content = _media_view(kind, quoted)
    elif kind == "pdf":
        content = _card("PDF documents are not rendered inline.", quoted, label="Download PDF")
    elif kind == "binary":
        content = _card("This looks like a binary file, so there is nothing to preview.", quoted)
    elif stat.st_size > max_bytes:
        content = _card(
            f"This file is {html.escape(listing.human_size(stat.st_size))}, larger than the "
            f"{html.escape(listing.human_size(max_bytes))} preview limit.",
            quoted,
        )
    else:
        with open(fs_path, "rb") as handle:
            data = handle.read(max_bytes + 1)
        text = _decode(data)
        if text is None:
            content = _card(
                "This file contains binary data, so there is nothing to preview.", quoted
            )
        elif mode == "source" or kind in ("code", "text"):
            content = _source_view(text, name)
        elif kind == "markdown":
            content = f'<div class="md">{_markdown.render(text)}</div>'
        elif kind == "json":
            content = _json_view(text)
        elif kind == "table":
            content = _table_view(text, name)
        elif kind == "notebook":
            content = _notebook_view(text)
        else:  # pragma: no cover - every kind above is covered
            content = _source_view(text, name)

    language = _highlight.language_for(name)
    badges = [html.escape(listing.human_size(stat.st_size))]
    if language and language != "text":
        badges.append(html.escape(_highlight.LANGUAGE_NAMES.get(language, language)))
    elif kind != "binary":
        badges.append(html.escape(kind))

    document = _TEMPLATE.format(
        style=_CSS + _highlight.CSS + _markdown.CSS,
        data_theme=html.escape(theme, quote=True),
        title=html.escape(name),
        breadcrumb=listing.breadcrumb(url_path),
        badges=" · ".join(f"<span>{badge}</span>" for badge in badges),
        modes=_mode_links(kind, mode, quoted),
        metadata=_metadata_panel(meta),
        content=content,
        quoted=quoted,
    )
    return document.encode("utf-8", "surrogateescape")


_CSS = """
:root { color-scheme: light dark; --accent: #2563eb; }
html[data-theme="light"] { color-scheme: light; }
html[data-theme="dark"] { color-scheme: dark; }
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 56rem; padding: 0 1rem; }
a { color: inherit; text-decoration: none; }
a:hover { text-decoration: underline; }
a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 2px; }
.topbar { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem;
  flex-wrap: wrap; }
h1.crumbs { font-size: 1.05rem; font-weight: 600; word-break: break-all; margin: 0; }
h1.crumbs a { color: var(--accent); }
h1.crumbs .sep, h1.crumbs .here { opacity: 0.6; font-weight: 400; }
.badges { font-size: 0.78rem; opacity: 0.65; white-space: nowrap; }
.bar { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem;
  flex-wrap: wrap; margin: 0.9rem 0; }
nav.modes { display: flex; font-size: 0.78rem; border-radius: 0.4rem; overflow: hidden;
  border: 1px solid color-mix(in srgb, currentColor 25%, transparent); }
nav.modes a { padding: 0.25rem 0.7rem; opacity: 0.65; }
nav.modes a.active { opacity: 1; background: color-mix(in srgb, currentColor 12%, transparent); }
.actions { display: flex; gap: 0.4rem; font-size: 0.78rem; }
.button { padding: 0.3rem 0.75rem; border-radius: 0.4rem; background: var(--accent); color: #fff;
  border: 1px solid transparent; }
.button.ghost { background: transparent; color: inherit;
  border-color: color-mix(in srgb, currentColor 25%, transparent); }
details.meta { margin: 0.9rem 0; padding: 0.6rem 0.9rem; border-radius: 0.5rem;
  background: color-mix(in srgb, currentColor 5%, transparent); font-size: 0.85rem; }
details.meta summary { cursor: pointer; font-weight: 600; opacity: 0.75; }
details.meta dl { display: grid; grid-template-columns: minmax(6rem, max-content) 1fr;
  gap: 0.25rem 1rem; margin: 0.7rem 0 0; }
details.meta dl > div { display: contents; }
details.meta dt { opacity: 0.6; text-transform: capitalize; }
details.meta dd { margin: 0; overflow-wrap: anywhere; }
.card { margin: 2rem 0; padding: 1.5rem; border-radius: 0.5rem; text-align: center;
  border: 1px dashed color-mix(in srgb, currentColor 25%, transparent); }
.card p { margin: 0.5rem 0; }
p.media { margin: 1rem 0; text-align: center; }
p.media img, p.media video { max-width: 100%; height: auto; border-radius: 0.4rem; }
p.media audio { width: 100%; max-width: 32rem; }
.tablewrap { overflow-x: auto; }
table.data { border-collapse: collapse; font-size: 0.85rem; }
table.data th, table.data td { padding: 0.3rem 0.7rem; text-align: left; white-space: nowrap;
  border: 1px solid color-mix(in srgb, currentColor 15%, transparent); }
table.data thead th { position: sticky; top: 0; background: Canvas; }
pre.out { margin: 0 0 0.8rem; padding: 0.6rem 1rem; overflow-x: auto; font-size: 0.82rem;
  border-left: 3px solid color-mix(in srgb, currentColor 25%, transparent); opacity: 0.85;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.cell { margin: 0.9rem 0; }
p.note, p.empty { font-size: 0.8rem; opacity: 0.6; }
footer { margin-top: 2rem; font-size: 0.8rem; opacity: 0.6; }
"""

_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en" data-theme="{data_theme}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} \N{MIDDLE DOT} servery</title>
<style>{style}</style>
</head>
<body>
<div class="topbar">
<h1 class="crumbs">{breadcrumb}</h1>
<div class="badges">{badges}</div>
</div>
<div class="bar">
{modes}
<div class="actions">
<a class="button ghost" href="{quoted}">Raw</a>
<a class="button" href="{quoted}?download=1" download>Download</a>
</div>
</div>
{metadata}
<main>
{content}
</main>
<footer><a href="./">\N{UPWARDS ARROW} Back to the directory</a> \N{MIDDLE DOT} \
preview by servery</footer>
</body>
</html>
"""
