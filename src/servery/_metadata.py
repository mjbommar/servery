"""Bounded, read-only document metadata extraction (opt-in ``--metadata``).

A directory listing knows a file's name, size, and mtime. This module adds what
is *inside* it — a Markdown front-matter title, a Python module docstring and
``__version__``, a LaTeX ``\\title{}``, an HTML ``<title>``, PEP 621 project
fields, a notebook's kernel, image dimensions, an MP3's ID3 tags, a PDF's Info
dictionary — normalized onto one small record so the listing can *show*, *filter*,
and *sort* by it, and so ``?metadata=1`` can hand it back as JSON.

Three rules keep this safe and cheap:

* **Bounded** — at most ``max_bytes`` (default 64 KiB) is read per file, and the
  result is cached on ``(path, mtime, size)``, so a re-sort of a big directory
  costs no I/O.
* **Never executes** — ``ast.parse`` builds a tree without running it, and
  ``json``/``tomllib`` are data parsers. No file is imported, no code is run.
* **Never raises** — a corrupt, truncated, or hostile file yields an empty
  record. A metadata column must not be able to 500 a directory listing.
"""

from __future__ import annotations

import ast
import contextlib
import csv
import dataclasses
import functools
import html.parser
import io
import json
import mimetypes
import os
import re
import stat as stat_module
import struct
import time
import tomllib
import urllib.parse
from collections.abc import Callable, Iterable
from typing import Any

from servery import _markdown

# Default per-file read budget. Front matter, docstrings, and HTML <head>s live
# in the first few KiB; this is generous for all of them.
DEFAULT_MAX_BYTES = 64 * 1024

# Field-length caps, so one pathological file cannot bloat a listing page.
_MAX_TITLE = 200
_MAX_DESCRIPTION = 400
_MAX_TAGS = 16

_WHITESPACE_RE = re.compile(r"\s+")


@dataclasses.dataclass(frozen=True, slots=True)
class Meta:
    """Normalized document metadata. Every field is optional."""

    kind: str = ""
    title: str = ""
    author: str = ""
    description: str = ""
    date: str = ""
    version: str = ""
    lang: str = ""
    tags: tuple[str, ...] = ()
    extra: tuple[tuple[str, str], ...] = ()

    def __bool__(self) -> bool:
        return bool(
            self.title
            or self.author
            or self.description
            or self.date
            or self.version
            or self.lang
            or self.tags
            or self.extra
        )

    def field(self, name: str) -> str:
        """One named field as text ("tag"/"tags" joins the list); "" if unset."""
        if name in ("tag", "tags"):
            return ", ".join(self.tags)
        value = getattr(self, name, "")
        return value if isinstance(value, str) else ""

    def haystack(self) -> str:
        """Everything searchable, lowercased — backs a bare ``?meta=`` query."""
        parts = [self.title, self.author, self.description, self.date, self.version, self.lang]
        parts.extend(self.tags)
        parts.extend(value for _, value in self.extra)
        return " ".join(parts).lower()

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready mapping with the unset fields omitted."""
        out: dict[str, Any] = {}
        for name in ("kind", "title", "author", "description", "date", "version", "lang"):
            value = getattr(self, name)
            if value:
                out[name] = value
        if self.tags:
            out["tags"] = list(self.tags)
        if self.extra:
            out["extra"] = dict(self.extra)
        return out


EMPTY = Meta()

# The metadata fields a listing can filter or sort on.
FIELDS = ("title", "author", "description", "date", "version", "lang", "tag")


# --- small helpers -------------------------------------------------------


def _clean(value: object, limit: int = _MAX_TITLE) -> str:
    """Collapse whitespace and truncate; non-scalars become ""."""
    if value is None or isinstance(value, bool | dict):
        return ""
    if isinstance(value, list | tuple):
        value = ", ".join(_clean(item, limit) for item in value if item is not None)
    text = _WHITESPACE_RE.sub(" ", str(value)).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"
    return text


def _tags(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        items: Iterable[object] = re.split(r"[,;]", value)
    elif isinstance(value, list | tuple):
        items = value
    else:
        return ()
    out = []
    for item in items:
        tag = _clean(item, 48)
        if tag and tag not in out:
            out.append(tag)
        if len(out) >= _MAX_TAGS:
            break
    return tuple(out)


_TITLE_KEYS = ("title", "name", "heading")
_AUTHOR_KEYS = ("author", "authors", "by", "creator", "artist", "maintainer", "maintainers")
_DESCRIPTION_KEYS = ("description", "summary", "subtitle", "abstract", "excerpt", "tagline")
_DATE_KEYS = ("date", "published", "pubdate", "created", "updated", "modified")
_VERSION_KEYS = ("version", "revision")
_LANG_KEYS = ("lang", "language", "locale")
_TAG_KEYS = ("tags", "keywords", "categories", "topics", "labels")


def _from_mapping(kind: str, data: dict[str, object]) -> Meta:
    """Build a Meta from a key/value mapping using the common field aliases."""
    lowered = {str(key).lower(): value for key, value in data.items()}

    def pick(keys: tuple[str, ...], limit: int = _MAX_TITLE) -> str:
        for key in keys:
            if key in lowered:
                text = _clean(lowered[key], limit)
                if text:
                    return text
        return ""

    tags: tuple[str, ...] = ()
    for key in _TAG_KEYS:
        if key in lowered:
            tags = _tags(lowered[key])
            if tags:
                break
    return Meta(
        kind=kind,
        title=pick(_TITLE_KEYS),
        author=pick(_AUTHOR_KEYS),
        description=pick(_DESCRIPTION_KEYS, _MAX_DESCRIPTION),
        date=pick(_DATE_KEYS, 64),
        version=pick(_VERSION_KEYS, 64),
        lang=pick(_LANG_KEYS, 32),
        tags=tags,
    )


def _merge(primary: Meta, fallback: Meta) -> Meta:
    """Fill ``primary``'s empty fields from ``fallback``."""
    return Meta(
        kind=primary.kind or fallback.kind,
        title=primary.title or fallback.title,
        author=primary.author or fallback.author,
        description=primary.description or fallback.description,
        date=primary.date or fallback.date,
        version=primary.version or fallback.version,
        lang=primary.lang or fallback.lang,
        tags=primary.tags or fallback.tags,
        extra=primary.extra or fallback.extra,
    )


def _text(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def _counts(text: str, truncated: bool) -> tuple[tuple[str, str], ...]:
    if truncated:
        return ()  # a partial read would report misleading totals
    return (("lines", str(text.count("\n") + 1)), ("words", str(len(text.split()))))


# --- a minimal YAML reader (front matter only) ---------------------------

_YAML_KEY_RE = re.compile(r"^([\w.\"'][\w.\" '-]*)[ \t]*:[ \t]*(.*)$")
_YAML_ITEM_RE = re.compile(r"^[ \t]*-[ \t]+(.*)$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    # An unquoted scalar may carry a trailing comment.
    return re.sub(r"[ \t]+#.*$", "", value).strip()


def parse_front_matter(fence: str, front: str) -> dict[str, object]:
    """Parse YAML-ish (``---``) or TOML (``+++``) front matter into a mapping.

    The YAML side is deliberately *not* a YAML implementation: top-level
    ``key: value`` scalars, ``[a, b]`` flow lists, and ``- item`` block lists are
    understood; anything more nested is skipped rather than guessed at.
    """
    if fence == "+++":
        try:
            return dict(tomllib.loads(front))
        except (tomllib.TOMLDecodeError, ValueError):
            return {}
    data: dict[str, object] = {}
    pending: list[str] | None = None
    key = ""
    for line in front.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = _YAML_ITEM_RE.match(line)
        if item is not None and pending is not None:
            pending.append(_unquote(item.group(1)))
            continue
        match = _YAML_KEY_RE.match(line)
        if match is None:
            continue
        if line[:1].isspace():  # a nested mapping: not modelled
            pending = None
            continue
        key = _unquote(match.group(1)).lower()
        value = match.group(2).strip()
        if not value:
            pending = []
            data[key] = pending
            continue
        pending = None
        if value.startswith("[") and value.endswith("]"):
            data[key] = [_unquote(part) for part in value[1:-1].split(",") if part.strip()]
        else:
            data[key] = _unquote(value)
    return data


# --- per-format extractors ----------------------------------------------

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_SETEXT_TITLE_RE = re.compile(r"^ {0,3}(\S[^\n]*)\n {0,3}=+[ \t]*$", re.MULTILINE)


def _markdown_meta(data: bytes, path: str, truncated: bool) -> Meta:
    text = _text(data)
    fence, front, body = _markdown.split_front_matter(text)
    meta = _from_mapping("markdown", parse_front_matter(fence, front)) if fence else EMPTY
    title = meta.title
    if not title:
        heading = _HEADING_RE.search(body)
        if heading is not None:
            title = _clean(heading.group(2))
        else:
            setext = _SETEXT_TITLE_RE.search(body)
            if setext is not None:
                title = _clean(setext.group(1))
    description = meta.description or _first_paragraph(body)
    return Meta(
        kind="markdown",
        title=title,
        author=meta.author,
        description=description,
        date=meta.date,
        version=meta.version,
        lang=meta.lang,
        tags=meta.tags,
        extra=_counts(text, truncated),
    )


def _first_paragraph(body: str) -> str:
    """The first prose paragraph, for a description fallback."""
    collected: list[str] = []
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped:
            if collected:
                break
            continue
        if stripped.startswith(("#", ">", "```", "~~~", "|", "---", "===", "[!", "<")):
            if collected:
                break
            continue
        if re.match(r"^(?:[-*+]|\d{1,9}[.)])\s", stripped):
            break
        collected.append(stripped)
        if len(" ".join(collected)) > _MAX_DESCRIPTION:
            break
    # Strip the loudest inline markers so the listing shows prose, not syntax.
    text = re.sub(r"[*_`]", "", " ".join(collected))
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return _clean(text, _MAX_DESCRIPTION)


_PY_DUNDERS = {
    "__author__": "author",
    "__version__": "version",
    "__date__": "date",
    "__license__": "license",
    "__copyright__": "copyright",
    "__maintainer__": "author",
}


def _python_meta(data: bytes, path: str, truncated: bool) -> Meta:
    text = _text(data)
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return Meta(kind="python", extra=_counts(text, truncated))
    found: dict[str, object] = {}
    docstring = ast.get_docstring(tree) or ""
    classes = functions = 0
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes += 1
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions += 1
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                name = _PY_DUNDERS.get(getattr(target, "id", ""))
                if name and isinstance(node.value, ast.Constant):
                    found.setdefault(name, node.value.value)
    summary, _, rest = docstring.strip().partition("\n")
    extra = list(_counts(text, truncated))
    if classes:
        extra.append(("classes", str(classes)))
    if functions:
        extra.append(("functions", str(functions)))
    extra.extend(
        (name, _clean(found[name], 64)) for name in ("license", "copyright") if name in found
    )
    return Meta(
        kind="python",
        title=_clean(summary),
        author=_clean(found.get("author")),
        description=_clean(rest, _MAX_DESCRIPTION),
        date=_clean(found.get("date"), 64),
        version=_clean(found.get("version"), 64),
        extra=tuple(extra),
    )


_TEX_COMMENT_RE = re.compile(r"(?<!\\)%[^\n]*")
_TEX_CLEAN_RE = re.compile(r"\\[a-zA-Z@]+\*?|[{}]|\\\\")


def _tex_value(text: str, command: str) -> str:
    """The brace-delimited argument of ``\\command{…}``, brace-balanced."""
    start = text.find("\\" + command)
    while start != -1:
        brace = text.find("{", start)
        if brace == -1:
            return ""
        depth, position = 1, brace + 1
        while position < len(text) and depth:
            char = text[position]
            if char == "\\":
                position += 2
                continue
            depth += 1 if char == "{" else -1 if char == "}" else 0
            position += 1
        if depth == 0:
            return _clean(_TEX_CLEAN_RE.sub(" ", text[brace + 1 : position - 1]))
        start = text.find("\\" + command, start + 1)
    return ""


def _latex_meta(data: bytes, path: str, truncated: bool) -> Meta:
    text = _TEX_COMMENT_RE.sub("", _text(data))
    authors = _tex_value(text, "author").replace(" and ", ", ")
    extra: list[tuple[str, str]] = []
    documentclass = _tex_value(text, "documentclass")
    if documentclass:
        extra.append(("documentclass", documentclass))
    return Meta(
        kind="latex",
        title=_tex_value(text, "title"),
        author=authors,
        description=_tex_value(text, "abstract") or _tex_value(text, "subtitle"),
        date=_tex_value(text, "date")[:64],
        tags=_tags(_tex_value(text, "keywords")),
        extra=tuple(extra),
    )


class _HeadParser(html.parser.HTMLParser):
    """Collects <title>, <meta name=…>, and the <html lang> attribute."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.lang = ""
        self.meta: dict[str, str] = {}
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): (value or "") for name, value in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "html":
            self.lang = values.get("lang", "")
        elif tag == "meta":
            name = (values.get("name") or values.get("property") or "").lower()
            content = values.get("content", "")
            if name and content:
                self.meta.setdefault(name.rsplit(":", 1)[-1], content)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and len(self.title) < _MAX_TITLE:
            self.title += data


def _html_meta(data: bytes, path: str, truncated: bool) -> Meta:
    parser = _HeadParser()
    with contextlib.suppress(AssertionError, ValueError):  # malformed markup
        parser.feed(_text(data))
    meta = _from_mapping("html", dict(parser.meta))
    return _merge(
        Meta(kind="html", title=_clean(parser.title), lang=_clean(parser.lang, 32)),
        meta,
    )


def _json_meta(data: bytes, path: str, truncated: bool) -> Meta:
    try:
        parsed = json.loads(_text(data))
    except (ValueError, RecursionError):
        return Meta(kind="json")
    if not isinstance(parsed, dict):
        return Meta(
            kind="json", extra=(("items", str(len(parsed))),) if isinstance(parsed, list) else ()
        )
    meta = _from_mapping("json", parsed)
    if isinstance(parsed.get("author"), dict):
        meta = dataclasses.replace(meta, author=_clean(parsed["author"].get("name")))
    return meta


def _toml_meta(data: bytes, path: str, truncated: bool) -> Meta:
    try:
        parsed = tomllib.loads(_text(data))
    except (tomllib.TOMLDecodeError, ValueError):
        return Meta(kind="toml")
    table = parsed
    for section in ("project", "package", "tool"):  # PEP 621 / Cargo layouts
        nested = parsed.get(section)
        if isinstance(nested, dict):
            table = nested
            break
    meta = _from_mapping("toml", table)
    authors = table.get("authors")
    if isinstance(authors, list) and authors and isinstance(authors[0], dict):
        names = [str(entry.get("name", "")) for entry in authors if isinstance(entry, dict)]
        meta = dataclasses.replace(meta, author=_clean([name for name in names if name]))
    return meta


def _yaml_meta(data: bytes, path: str, truncated: bool) -> Meta:
    return _from_mapping("yaml", parse_front_matter("---", _text(data)))


def _notebook_meta(data: bytes, path: str, truncated: bool) -> Meta:
    try:
        parsed = json.loads(_text(data))
    except (ValueError, RecursionError):
        return Meta(kind="notebook")
    if not isinstance(parsed, dict):
        return Meta(kind="notebook")
    metadata = parsed.get("metadata") if isinstance(parsed.get("metadata"), dict) else {}
    kernel = metadata.get("kernelspec") if isinstance(metadata, dict) else None
    language = metadata.get("language_info") if isinstance(metadata, dict) else None
    cells = parsed.get("cells") if isinstance(parsed.get("cells"), list) else []
    title = ""
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("cell_type") != "markdown":
            continue
        source = cell.get("source")
        text = "".join(source) if isinstance(source, list) else str(source or "")
        heading = _HEADING_RE.search(text)
        if heading is not None:
            title = _clean(heading.group(2))
            break
    extra = [("cells", str(len(cells)))]
    if isinstance(kernel, dict) and kernel.get("display_name"):
        extra.append(("kernel", _clean(kernel["display_name"], 64)))
    if isinstance(language, dict) and language.get("name"):
        extra.append(("language", _clean(language["name"], 32)))
    base = _from_mapping("notebook", metadata if isinstance(metadata, dict) else {})
    return _merge(Meta(kind="notebook", title=title, extra=tuple(extra)), base)


def _csv_meta(data: bytes, path: str, truncated: bool) -> Meta:
    text = _text(data)
    delimiter = "\t" if path.lower().endswith(".tsv") else ","
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error:
        return Meta(kind="table")
    rows = [row for row in rows if row]
    if not rows:
        return Meta(kind="table")
    header = [_clean(cell, 48) for cell in rows[0][:_MAX_TAGS]]
    extra = [("columns", str(len(rows[0])))]
    if not truncated:
        extra.append(("rows", str(max(0, len(rows) - 1))))
    return Meta(
        kind="table",
        description=_clean(", ".join(cell for cell in header if cell), _MAX_DESCRIPTION),
        tags=tuple(cell for cell in header if cell),
        extra=tuple(extra),
    )


def _rst_meta(data: bytes, path: str, truncated: bool) -> Meta:
    text = _text(data)
    title = ""
    lines = text.split("\n")
    for index, line in enumerate(lines[:80]):
        stripped = line.strip()
        if len(stripped) >= 3 and len(set(stripped)) == 1 and stripped[0] in "=-~`^\"'*+#":
            candidate = lines[index - 1].strip() if index else ""
            following = lines[index + 1].strip() if index + 1 < len(lines) else ""
            if candidate and len(stripped) >= len(candidate) - 2:
                title = _clean(candidate)
                break
            if not candidate and following:  # overline style
                title = _clean(following)
                break
    fields: dict[str, object] = {}
    for line in lines[:200]:
        match = re.match(r"^:([\w -]+):[ \t]+(.+)$", line)
        if match is not None:
            fields.setdefault(match.group(1).strip().lower(), match.group(2).strip())
    return _merge(
        Meta(kind="rst", title=title, extra=_counts(text, truncated)),
        _from_mapping("rst", fields),
    )


def _text_meta(data: bytes, path: str, truncated: bool) -> Meta:
    text = _text(data)
    title = ""
    if path.lower().endswith((".txt", ".text")):
        first = next((line.strip() for line in text.split("\n") if line.strip()), "")
        if 0 < len(first) <= 120:
            title = _clean(first)
    return Meta(kind="text", title=title, extra=_counts(text, truncated))


def _source_meta(data: bytes, path: str, truncated: bool) -> Meta:
    """Line/word counts for source files with no format-specific extractor."""
    return Meta(kind="source", extra=_counts(_text(data), truncated))


# --- binary formats ------------------------------------------------------

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PNG_TEXT_KEYS = {b"Title": "title", b"Author": "author", b"Description": "description"}


def _png_meta(data: bytes) -> Meta:
    if len(data) < 24 or not data.startswith(_PNG_MAGIC) or data[12:16] != b"IHDR":
        return EMPTY
    width, height = struct.unpack(">II", data[16:24])
    found: dict[str, str] = {}
    offset = 8
    while offset + 8 <= len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        if kind in (b"tEXt", b"iTXt") and b"\x00" in body:
            key, _, value = body.partition(b"\x00")
            name = _PNG_TEXT_KEYS.get(key)
            if name:
                found.setdefault(name, value.lstrip(b"\x00").decode("latin-1", "replace"))
        if kind == b"IDAT" or length > len(data):
            break
        offset += 12 + length
    return _merge(
        _from_mapping("image", dict(found)),
        Meta(kind="image", extra=(("width", str(width)), ("height", str(height)))),
    )


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or not data.startswith((b"GIF87a", b"GIF89a")):
        return None
    return struct.unpack("<HH", data[6:10])


def _bmp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 26 or not data.startswith(b"BM"):
        return None
    width, height = struct.unpack("<ii", data[18:26])
    return abs(width), abs(height)


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    offset = 2
    while offset + 9 < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        (length,) = struct.unpack(">H", data[offset + 2 : offset + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
            return width, height
        offset += 2 + length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    # 21 bytes covers the shortest header we can read (VP8L); the wider variants
    # re-check their own length below.
    if len(data) < 21 or not data.startswith(b"RIFF") or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        start = data.find(b"\x9d\x01\x2a", 20, 40)
        if start == -1:
            return None
        width, height = struct.unpack("<HH", data[start + 3 : start + 7])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


_SVG_TAG_RE = re.compile(rb"<svg\b[^>]*>", re.IGNORECASE | re.DOTALL)
_SVG_ATTR_RE = re.compile(rb'(width|height|viewBox)\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
_SVG_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _svg_meta(data: bytes) -> Meta:
    tag = _SVG_TAG_RE.search(data)
    if tag is None:
        return EMPTY
    attributes = {
        name.lower().decode(): value.decode("utf-8", "replace")
        for name, value in _SVG_ATTR_RE.findall(tag.group())
    }
    extra: list[tuple[str, str]] = []
    width, height = attributes.get("width", ""), attributes.get("height", "")
    if not (width and height) and "viewbox" in attributes:
        parts = attributes["viewbox"].replace(",", " ").split()
        if len(parts) == 4:
            width, height = parts[2], parts[3]
    if width:
        extra.append(("width", _clean(width, 24)))
    if height:
        extra.append(("height", _clean(height, 24)))
    title = _SVG_TITLE_RE.search(data)
    return Meta(
        kind="image",
        title=_clean(title.group(1).decode("utf-8", "replace")) if title else "",
        extra=tuple(extra),
    )


def _image_meta(data: bytes, path: str, truncated: bool) -> Meta:
    if path.lower().endswith(".svg"):
        return _svg_meta(data)
    if data.startswith(_PNG_MAGIC):
        return _png_meta(data)
    size = _jpeg_size(data) or _gif_size(data) or _webp_size(data) or _bmp_size(data)
    if size is None:
        return EMPTY
    return Meta(kind="image", extra=(("width", str(size[0])), ("height", str(size[1]))))


_ID3_FRAMES = {
    b"TIT2": "title",
    b"TPE1": "author",
    b"TALB": "album",
    b"TDRC": "date",
    b"TYER": "date",
    b"TCON": "genre",
    b"TRCK": "track",
    b"COMM": "description",
}


def _id3_text(payload: bytes) -> str:
    if not payload:
        return ""
    encoding, body = payload[0], payload[1:]
    if encoding == 0:
        text = body.decode("latin-1", "replace")
    elif encoding == 1:
        text = body.decode("utf-16", "replace")
    elif encoding == 2:
        text = body.decode("utf-16-be", "replace")
    else:
        text = body.decode("utf-8", "replace")
    return _clean(text.replace("\x00", " "))


def _id3_meta(data: bytes, path: str, truncated: bool) -> Meta:
    if len(data) < 10 or not data.startswith(b"ID3"):
        return EMPTY
    major = data[3]
    size = 0
    for byte in data[6:10]:  # syncsafe integer
        size = (size << 7) | (byte & 0x7F)
    end = min(10 + size, len(data))
    offset = 10
    found: dict[str, str] = {}
    while offset + 10 <= end:
        frame = data[offset : offset + 4]
        if not frame.strip(b"\x00"):
            break
        if major >= 4:
            length = 0
            for byte in data[offset + 4 : offset + 8]:
                length = (length << 7) | (byte & 0x7F)
        else:
            (length,) = struct.unpack(">I", data[offset + 4 : offset + 8])
        if length <= 0 or offset + 10 + length > end:
            break
        name = _ID3_FRAMES.get(frame)
        if name:
            found.setdefault(name, _id3_text(data[offset + 10 : offset + 10 + length]))
        offset += 10 + length
    if not found:
        return EMPTY
    extra = [(key, found[key]) for key in ("album", "track") if found.get(key)]
    return Meta(
        kind="audio",
        title=found.get("title", ""),
        author=found.get("author", ""),
        description=found.get("description", ""),
        date=found.get("date", "")[:64],
        tags=_tags(found.get("genre", "")),
        extra=tuple(extra),
    )


_PDF_INFO_RE = re.compile(
    rb"/(Title|Author|Subject|Keywords|Producer|Creator)\s*(\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]*>)"
)
_PDF_VERSION_RE = re.compile(rb"%PDF-(\d\.\d)")
_PDF_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}


def _pdf_string(raw: bytes) -> str:
    if raw.startswith(b"<"):
        try:
            raw = bytes.fromhex(raw[1:-1].decode("ascii").replace(" ", "").replace("\n", ""))
        except ValueError:
            return ""
    else:
        body = raw[1:-1]
        out = bytearray()
        index = 0
        while index < len(body):
            char = body[index : index + 1]
            if char == b"\\" and index + 1 < len(body):
                nxt = body[index + 1 : index + 2]
                out += _PDF_ESCAPES.get(nxt, nxt)
                index += 2
                continue
            out += char
            index += 1
        raw = bytes(out)
    if raw.startswith(b"\xfe\xff"):
        return _clean(raw[2:].decode("utf-16-be", "replace"))
    return _clean(raw.decode("latin-1", "replace"))


def _pdf_meta(data: bytes, path: str, truncated: bool) -> Meta:
    # The Info dictionary usually sits near the *end* of the file, so scan the
    # head (for the version) plus a bounded tail.
    blob = data + _read_tail(path, 64 * 1024)
    found: dict[str, object] = {}
    for key, raw in _PDF_INFO_RE.findall(blob):
        value = _pdf_string(raw)
        if value:
            found.setdefault(key.decode("ascii").lower(), value)
    extra: list[tuple[str, str]] = []
    version = _PDF_VERSION_RE.match(data)
    if version is not None:
        extra.append(("pdf_version", version.group(1).decode("ascii")))
    extra.extend((name, str(found[name])[:64]) for name in ("producer", "creator") if name in found)
    meta = _from_mapping("pdf", found)
    return dataclasses.replace(meta, kind="pdf", extra=tuple(extra))


def _read_tail(path: str, count: int) -> bytes:
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(0, size - count))
            return handle.read(count)
    except OSError:  # pragma: no cover - raced/unreadable file
        return b""


# --- dispatch ------------------------------------------------------------

_Extractor = Callable[[bytes, str, bool], Meta]

_BY_EXTENSION: dict[str, _Extractor] = {
    "bmp": _image_meta,
    "cfg": _yaml_meta,
    "csv": _csv_meta,
    "gif": _image_meta,
    "htm": _html_meta,
    "html": _html_meta,
    "ipynb": _notebook_meta,
    "jpeg": _image_meta,
    "jpg": _image_meta,
    "json": _json_meta,
    "latex": _latex_meta,
    "log": _text_meta,
    "markdown": _markdown_meta,
    "md": _markdown_meta,
    "mdown": _markdown_meta,
    "mkd": _markdown_meta,
    "mp3": _id3_meta,
    "pdf": _pdf_meta,
    "png": _image_meta,
    "py": _python_meta,
    "pyi": _python_meta,
    "rst": _rst_meta,
    "svg": _image_meta,
    "sty": _latex_meta,
    "tex": _latex_meta,
    "toml": _toml_meta,
    "tsv": _csv_meta,
    "txt": _text_meta,
    "webp": _image_meta,
    "xhtml": _html_meta,
    "yaml": _yaml_meta,
    "yml": _yaml_meta,
}

# Everything else that is plainly source: counts only, no format guessing.
_SOURCE_EXTENSIONS = frozenset(
    {
        "c",
        "cc",
        "cpp",
        "cs",
        "css",
        "go",
        "h",
        "hpp",
        "hs",
        "java",
        "jl",
        "js",
        "jsx",
        "kt",
        "lua",
        "m",
        "mjs",
        "php",
        "pl",
        "pm",
        "r",
        "rb",
        "rs",
        "scm",
        "sh",
        "sql",
        "swift",
        "ts",
        "tsx",
        "vue",
        "xml",
        "zsh",
    }
)


def _extension(path: str) -> str:
    base = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    head, dot, ext = base.rpartition(".")
    return ext if dot and head else ""


def extractor_for(path: str) -> _Extractor | None:
    """The extractor for ``path``, or ``None`` when the type is not covered."""
    ext = _extension(path)
    found = _BY_EXTENSION.get(ext)
    if found is not None:
        return found
    if ext in _SOURCE_EXTENSIONS:
        return _source_meta
    return None


def extract(path: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Meta:
    """Extract metadata for ``path``; ``EMPTY`` when unsupported or unreadable.

    Never raises: every failure mode (unknown type, unreadable file, malformed
    content) collapses to an empty record.
    """
    extractor = extractor_for(path)
    if extractor is None:
        return EMPTY
    try:
        with open(path, "rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        return EMPTY
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    try:
        return extractor(data, path, truncated)
    except Exception:  # a metadata column must never break a listing
        return EMPTY


def for_file(path: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Meta:
    """Cached :func:`extract`, keyed on the file's identity *and* its mtime/size."""
    try:
        stat = os.stat(path)
    except OSError:
        return EMPTY
    return _cached(path, stat.st_mtime_ns, stat.st_size, max_bytes)


@functools.lru_cache(maxsize=4096)
def _cached(path: str, mtime_ns: int, size: int, max_bytes: int) -> Meta:
    return extract(path, max_bytes=max_bytes)


def cache_clear() -> None:
    """Drop the extraction cache (tests, and long-lived servers under churn)."""
    _cached.cache_clear()


# --- JSON views (?metadata=1) -------------------------------------------

# Upper bound on entries in a directory metadata document, mirroring the
# listing's own scan cap: an extraction per entry must stay bounded work.
MAX_ENTRIES = 5000


def _timestamp(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def describe(fs_path: str, url_path: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """A JSON-ready record for one file: identity, stat data, and metadata."""
    name = os.path.basename(url_path.rstrip("/")) or os.path.basename(fs_path)
    entry: dict[str, Any] = {"name": name, "path": url_path}
    try:
        stat = os.stat(fs_path)
    except OSError:
        return entry
    directory = stat_module.S_ISDIR(stat.st_mode)
    entry["type"] = "directory" if directory else "file"
    entry["modified"] = _timestamp(stat.st_mtime)
    if directory:
        return entry
    entry["size"] = stat.st_size
    entry["content_type"] = mimetypes.guess_file_type(fs_path)[0] or "application/octet-stream"
    meta = for_file(fs_path, max_bytes=max_bytes).to_dict()
    if meta:
        entry["metadata"] = meta
    return entry


def describe_directory(
    fs_dir: str,
    url_path: str,
    *,
    show_hidden: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    limit: int = MAX_ENTRIES,
) -> dict[str, Any]:
    """A JSON-ready metadata index of ``fs_dir``: one record per entry.

    Directories first, then names — the same order the HTML listing uses by
    default — so the JSON and the page agree. Raises ``OSError`` if the
    directory cannot be scanned.
    """
    names: list[tuple[bool, str]] = []
    with os.scandir(fs_dir) as scan:
        for entry in scan:
            if not show_hidden and entry.name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:  # pragma: no cover - raced entry
                is_dir = False
            names.append((not is_dir, entry.name))
            if len(names) > limit:
                break
    truncated = len(names) > limit
    names.sort(key=lambda item: (item[0], item[1].lower()))
    entries = [
        describe(
            os.path.join(fs_dir, name),
            url_path + urllib.parse.quote(name, errors="surrogatepass"),
            max_bytes=max_bytes,
        )
        for _, name in names[:limit]
    ]
    document: dict[str, Any] = {"path": url_path, "count": len(entries), "entries": entries}
    if truncated:
        document["truncated"] = True
    return document


def to_json(document: dict[str, Any]) -> bytes:
    """Serialize a describe/describe_directory document to UTF-8 JSON bytes."""
    return json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8", "surrogateescape")
