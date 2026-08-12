"""A small, safe Markdown-subset renderer for the opt-in ``--preview`` mode.

The standard library has no Markdown parser, and servery will never take a
dependency to get one (see ``docs/PRINCIPLES.md``). What it *can* do — the same
call it made for QR codes and HPACK — is write a bounded one in-house and be
honest about the boundary. This is a **CommonMark subset**, not a conformant
implementation:

* blocks — ATX + setext headings, fenced (with syntax highlighting) and indented
  code, blockquotes, ordered/unordered/task lists, GFM tables, thematic breaks,
  link reference definitions, paragraphs, YAML/TOML front matter (skipped);
* inline — code spans, emphasis/strong/strikethrough, links (inline, reference,
  autolink, bare URL), images, hard breaks, backslash escapes.

**Raw HTML is escaped, never passed through.** That is the whole security model:
the output is built from a fixed tag vocabulary, every text run goes through
``html.escape``, and link/image URLs must pass a scheme allowlist
(:func:`safe_url`) so ``javascript:`` and ``data:`` cannot survive. A Markdown
file uploaded by an untrusted party therefore cannot inject script into the
preview page — and the strict CSP on generated pages is a second wall behind it.
"""

from __future__ import annotations

import dataclasses
import html
import re

from servery import _highlight

# --- block-level patterns ------------------------------------------------

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*(.*?)[ \t]*$")
_ATX_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_HR_RE = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
_BQ_RE = re.compile(r"^ {0,3}>[ \t]?(.*)$")
_UL_RE = re.compile(r"^( *)([-*+])([ \t]+)(.*)$")
_OL_RE = re.compile(r"^( *)(\d{1,9})([.)])([ \t]+)(.*)$")
_SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_REF_RE = re.compile(
    r"""^[ ]{0,3}\[([^\]]+)\]:[ \t]*<?([^\s>]*)>?
        (?:[ \t]+(?:"([^"]*)"|'([^']*)'|\(([^)]*)\)))?[ \t]*$""",
    re.VERBOSE,
)
_TABLE_DELIM_RE = re.compile(r"^ {0,3}\|?(?:[ \t]*:?-+:?[ \t]*\|)+[ \t]*:?-*:?[ \t]*\|?[ \t]*$")
_TASK_RE = re.compile(r"^\[([ xX])\][ \t]+(.*)$", re.S)
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")

# Front matter: a fenced block at the very top. Required to *look* like key/value
# data, so a document that merely opens with a thematic break is not eaten.
_FRONT_RE = re.compile(
    r"\A(---|\+\+\+)[ \t]*\n(.*?)\n\1[ \t]*(?:\n|\Z)",
    re.DOTALL,
)
_FRONT_KEY_RE = re.compile(r"^[ \t]*[\w.\"'-]+[ \t]*[:=]")

# --- inline patterns -----------------------------------------------------

_INLINE_SCAN = re.compile(
    r"(?P<esc>\\[!-/:-@\[-`{-~])"
    r"|(?P<code>`++)"
    r"|(?P<auto><(?:[a-zA-Z][\w+.-]*:[^<>\s]+|[^<>\s@]+@[^<>\s]+)>)"
    r"|(?P<image>!\[)"
    r"|(?P<link>\[)"
    r"|(?P<br>(?:  +|\\)\n)"
    r"|(?P<url>(?<![\w@.:/])(?:https?://|www\.)[^\s<>`\[\]()]++)"
)
_DEST_RE = re.compile(
    r"""\([ \t\n]*(?:<([^<>\n]*)>|((?:[^\s()\\]|\\.|\([^\s()]*+\))*+))
        (?:[ \t\n]+(?:"([^"]*)"|'([^']*)'))?[ \t\n]*\)""",
    re.VERBOSE,
)
# Applied to text that is already HTML-escaped, so these can never see a "<".
_STRIKE_RE = re.compile(r"~~(?!\s)(.+?)(?<!\s)~~", re.S)
_STRONG_STAR_RE = re.compile(r"\*\*(?!\s)((?:[^*]|\*(?!\*))+?)(?<!\s)\*\*", re.S)
_STRONG_LINE_RE = re.compile(r"(?<![\w\\])__(?!\s)(.+?)(?<!\s)__(?!\w)", re.S)
_EM_STAR_RE = re.compile(r"(?<!\\)\*(?!\s)([^*]+?)(?<!\s)\*", re.S)
_EM_LINE_RE = re.compile(r"(?<![\w\\])_(?!\s)([^_]+?)(?<!\s)_(?!\w)", re.S)

# URL schemes allowed in href/src. Everything else (javascript:, data:, vbscript:)
# is dropped; a URL with no scheme at all is relative and always allowed.
_SAFE_SCHEMES = frozenset(("http", "https", "mailto", "ftp", "ftps", "tel", "irc", "news"))
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")
_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")
_SLUG_STRIP_RE = re.compile(r"[^\w\- ]+")


def split_front_matter(text: str) -> tuple[str, str, str]:
    """Split leading YAML/TOML front matter off ``text``.

    Returns ``(fence, front_matter, body)``; ``fence`` is ``""`` when there is
    none. Shared with :mod:`servery._metadata`, which parses what this skips.
    """
    match = _FRONT_RE.match(text.replace("\r\n", "\n").replace("\r", "\n"))
    if match is None:
        return "", "", text
    front = match.group(2)
    first = next((line for line in front.split("\n") if line.strip()), "")
    if not _FRONT_KEY_RE.match(first):
        return "", "", text  # a thematic break / setext rule, not front matter
    return match.group(1), front, text[match.end() :]


def safe_url(url: str) -> str | None:
    """Return ``url`` if its scheme is allowed (or it has none), else ``None``.

    Control characters and whitespace are stripped before the scheme is read, so
    ``java\\tscript:alert(1)`` cannot smuggle a blocked scheme past the check.
    """
    cleaned = _CONTROL_RE.sub("", url).strip()
    if not cleaned:
        return None
    match = _SCHEME_RE.match(cleaned)
    if match is None:
        return cleaned  # relative, fragment, or protocol-relative
    if match.group(1).lower() in _SAFE_SCHEMES:
        return cleaned
    return None


@dataclasses.dataclass(slots=True)
class _Context:
    """Per-render state: reference definitions and heading-id uniqueness."""

    refs: dict[str, tuple[str, str]] = dataclasses.field(default_factory=dict)
    slugs: dict[str, int] = dataclasses.field(default_factory=dict)

    def slug(self, text: str) -> str:
        base = _SLUG_STRIP_RE.sub("", text.lower()).strip().replace(" ", "-")[:64] or "section"
        seen = self.slugs.get(base, 0)
        self.slugs[base] = seen + 1
        return base if not seen else f"{base}-{seen}"


def _normalize(text: str) -> list[str]:
    """Split into lines with leading tabs expanded (inner tabs left alone)."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = raw.lstrip(" \t")
        prefix = raw[: len(raw) - len(stripped)]
        lines.append(prefix.expandtabs(4) + stripped)
    return lines


def _column(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _dedent(line: str, columns: int) -> str:
    taken = 0
    while taken < columns and taken < len(line) and line[taken] == " ":
        taken += 1
    return line[taken:]


def _collect_refs(lines: list[str], ctx: _Context) -> None:
    """Pre-pass: record ``[label]: url "title"`` definitions outside code fences."""
    fence = ""
    for line in lines:
        if fence:
            if line.strip().startswith(fence):
                fence = ""
            continue
        if _is_fence(line):
            fence = line.strip()[:3]
            continue
        match = _REF_RE.match(line)
        if match is not None:
            title = match.group(3) or match.group(4) or match.group(5) or ""
            ctx.refs.setdefault(match.group(1).strip().lower(), (match.group(2), title))


# --- block parsing -------------------------------------------------------


def _is_fence(line: str) -> re.Match[str] | None:
    """The match for a ``` / ~~~ code-fence opener, else ``None``."""
    if line.strip()[:3] not in ("```", "~~~"):
        return None
    return _FENCE_RE.match(line)


def _closes_fence(line: str, marker: str) -> bool:
    """True when ``line`` is a closing fence for ``marker`` (same char, no info)."""
    text = line.strip()
    return text.startswith(marker) and not text.strip(marker[0])


def _is_heading(line: str) -> re.Match[str] | None:
    """The match for an ATX heading line, else ``None``."""
    if not line.lstrip().startswith("#"):
        return None
    return _ATX_RE.match(line)


def _heading(level: int, text: str, ctx: _Context) -> str:
    slug = html.escape(ctx.slug(text), quote=True)
    return f'<h{level} id="{slug}">{_inline(text, ctx)}</h{level}>'


def _starts_block(line: str) -> bool:
    """True when ``line`` interrupts an open paragraph."""
    return bool(
        _is_fence(line)
        or _is_heading(line)
        or _HR_RE.match(line)
        or _BQ_RE.match(line)
        or _UL_RE.match(line)
        or _OL_RE.match(line)
    )


def _parse(lines: list[str], ctx: _Context) -> list[tuple[str, str]]:
    """Parse ``lines`` into ``(kind, html)`` blocks; ``kind`` is "p" or "block"."""
    blocks: list[tuple[str, str]] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        fence = _is_fence(line)
        if fence is not None:
            marker, info = fence.group(1), fence.group(2)
            index += 1
            body: list[str] = []
            while index < total and not _closes_fence(lines[index], marker):
                body.append(_dedent(lines[index], _column(line)))
                index += 1
            index += 1  # closing fence (or EOF)
            blocks.append(("block", _code_block("\n".join(body), info)))
            continue

        if _HR_RE.match(line):
            blocks.append(("block", "<hr>"))
            index += 1
            continue

        atx = _is_heading(line)
        if atx is not None:
            level = len(atx.group(1))
            text = (atx.group(2) or "").strip()
            if text.endswith("#"):  # optional closing sequence
                text = re.sub(r"[ \t]#+$", "", text).strip()
            blocks.append(("block", _heading(level, text, ctx)))
            index += 1
            continue

        if _BQ_RE.match(line):
            quoted: list[str] = []
            while index < total:
                match = _BQ_RE.match(lines[index])
                if match is not None:
                    quoted.append(match.group(1))
                    index += 1
                    continue
                if lines[index].strip() and not _starts_block(lines[index]):
                    quoted.append(lines[index])  # lazy continuation
                    index += 1
                    continue
                break
            inner = "\n".join(html for _, html in _parse(quoted, ctx))
            blocks.append(("block", f"<blockquote>{inner}</blockquote>"))
            continue

        if _UL_RE.match(line) or _OL_RE.match(line):
            rendered, index = _parse_list(lines, index, ctx)
            blocks.append(("block", rendered))
            continue

        if "|" in line and index + 1 < total and _TABLE_DELIM_RE.match(lines[index + 1]):
            rendered, index = _parse_table(lines, index, ctx)
            blocks.append(("block", rendered))
            continue

        if _REF_RE.match(line):  # already captured by the pre-pass
            index += 1
            continue

        # An indented chunk at block level is code. It can never *interrupt* a
        # paragraph — the paragraph collector below already swallows an indented
        # line as a lazy continuation — so no "previous block" check is needed.
        if _column(line) >= 4:
            body = []
            while index < total and (not lines[index].strip() or _column(lines[index]) >= 4):
                body.append(_dedent(lines[index], 4))
                index += 1
            while body and not body[-1].strip():
                body.pop()
            blocks.append(("block", _code_block("\n".join(body), "")))
            continue

        paragraph: list[str] = []
        heading = 0
        while index < total:
            current = lines[index]
            if not current.strip():
                break
            setext = _SETEXT_RE.match(current) if paragraph else None
            if setext is not None:
                heading = 1 if setext.group(1)[0] == "=" else 2
                index += 1
                break
            if paragraph and _starts_block(current):
                break
            paragraph.append(current.lstrip())
            index += 1
        text = "\n".join(paragraph)
        if heading:
            blocks.append(("block", _heading(heading, text, ctx)))
        elif text:
            blocks.append(("p", f"<p>{_inline(text, ctx)}</p>"))
    return blocks


def _code_block(body: str, info: str) -> str:
    language = _highlight.language_for_info(info) if info else None
    rendered = _highlight.code_block(body, language, line_numbers=False)
    if language and language != "text":
        label = html.escape(_highlight.LANGUAGE_NAMES.get(language, language))
        return f'<div class="codewrap" data-lang="{label}">{rendered}</div>'
    return rendered


def _item_start(line: str) -> tuple[int, bool, str, int, str] | None:
    """``(indent, ordered, marker, content_column, rest)`` for a list-item line."""
    match = _UL_RE.match(line)
    if match is not None:
        indent = len(match.group(1))
        return indent, False, match.group(2), indent + 1 + len(match.group(3)), match.group(4)
    match = _OL_RE.match(line)
    if match is not None:
        indent = len(match.group(1))
        width = indent + len(match.group(2)) + len(match.group(3)) + len(match.group(4))
        return indent, True, match.group(2), width, match.group(5)
    return None


def _parse_list(lines: list[str], index: int, ctx: _Context) -> tuple[str, int]:
    """Collect one list (with nested content) starting at ``index``."""
    first = _item_start(lines[index])
    if first is None:  # pragma: no cover - the caller only enters on an item line
        return "", index + 1
    ordered = first[1]
    start = first[2] if ordered else ""
    items: list[list[str]] = []
    loose = False
    pending_blanks = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        head = _item_start(line)
        if head is not None and head[0] < 4 and head[1] == ordered:
            if items and pending_blanks:
                loose = True
            pending_blanks = 0
            content_column = head[3]
            items.append([head[4]])
            index += 1
            while index < total:
                nxt = lines[index]
                if not nxt.strip():
                    ahead = index
                    while ahead < total and not lines[ahead].strip():
                        ahead += 1
                    if ahead < total and _column(lines[ahead]) >= content_column:
                        items[-1].extend([""] * (ahead - index))
                        index = ahead
                        loose = True
                        continue
                    break
                if _column(nxt) >= content_column:
                    items[-1].append(_dedent(nxt, content_column))
                    index += 1
                    continue
                if _item_start(nxt) is not None or _starts_block(nxt):
                    break
                items[-1].append(nxt.lstrip())  # lazy paragraph continuation
                index += 1
            continue
        if not line.strip():
            pending_blanks += 1
            index += 1
            continue
        break

    rendered: list[str] = []
    for item in items:
        task = _TASK_RE.match(item[0]) if item else None
        classes = ""
        if task is not None:
            checked = " checked" if task.group(1) in "xX" else ""
            item = [task.group(2), *item[1:]]
            classes = ' class="task"'
            prefix = f'<input type="checkbox" disabled{checked}> '
        else:
            prefix = ""
        blocks = _parse(item, ctx)
        if not loose and blocks and blocks[0][0] == "p":
            body = blocks[0][1][3:-4] + "".join(chunk for _, chunk in blocks[1:])
        else:
            body = "".join(chunk for _, chunk in blocks)
        rendered.append(f"<li{classes}>{prefix}{body}</li>")

    body = "".join(rendered)
    if ordered:
        attribute = f' start="{int(start)}"' if start.isdigit() and start != "1" else ""
        return f"<ol{attribute}>{body}</ol>", index
    return f"<ul>{body}</ul>", index


def _split_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _CELL_SPLIT_RE.split(text)]


def _parse_table(lines: list[str], index: int, ctx: _Context) -> tuple[str, int]:
    header = _split_row(lines[index])
    alignments = []
    for cell in _split_row(lines[index + 1]):
        left, right = cell.startswith(":"), cell.endswith(":")
        alignments.append(
            "center" if left and right else "right" if right else "left" if left else ""
        )
    index += 2
    rows: list[list[str]] = []
    total = len(lines)
    while index < total and lines[index].strip() and "|" in lines[index]:
        rows.append(_split_row(lines[index]))
        index += 1

    def cell(tag: str, text: str, position: int) -> str:
        align = alignments[position] if position < len(alignments) else ""
        style = f' style="text-align:{align}"' if align else ""
        return f"<{tag}{style}>{_inline(text, ctx)}</{tag}>"

    head = "".join(cell("th", text, i) for i, text in enumerate(header))
    body = "".join(
        "<tr>" + "".join(cell("td", text, i) for i, text in enumerate(row)) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>", index


# --- inline parsing ------------------------------------------------------


def _emphasis(text: str) -> str:
    """Apply strike/strong/em to an already-escaped text run."""
    text = _STRIKE_RE.sub(r"<del>\1</del>", text)
    text = _STRONG_STAR_RE.sub(r"<strong>\1</strong>", text)
    text = _STRONG_LINE_RE.sub(r"<strong>\1</strong>", text)
    text = _EM_STAR_RE.sub(r"<em>\1</em>", text)
    return _EM_LINE_RE.sub(r"<em>\1</em>", text)


def _match_bracket(text: str, start: int) -> int:
    """Index of the ``]`` closing the ``[`` at ``start``-1, or -1."""
    depth = 1
    position = start
    while position < len(text):
        char = text[position]
        if char == "\\":
            position += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return position
        position += 1
    return -1


def _link_target(
    text: str, position: int, label: str, ctx: _Context
) -> tuple[str, str, int] | None:
    """Resolve the destination following a link label; ``(url, title, end)``."""
    if position < len(text) and text[position] == "(":
        match = _DEST_RE.match(text, position)
        if match is not None:
            url = match.group(1) or match.group(2) or ""
            title = match.group(3) or match.group(4) or ""
            return url, title, match.end()
        return None
    key = label.strip().lower()
    if position < len(text) and text[position] == "[":
        end = text.find("]", position + 1)
        if end != -1:
            reference = text[position + 1 : end].strip().lower() or key
            target = ctx.refs.get(reference)
            if target is not None:
                return target[0], target[1], end + 1
            return None
    target = ctx.refs.get(key)  # shortcut reference: [label]
    if target is not None:
        return target[0], target[1], position
    return None


def _anchor(url: str, title: str, body: str) -> str:
    destination = safe_url(url)
    if destination is None:
        return body  # blocked scheme: keep the text, drop the link
    attributes = f' title="{html.escape(title, quote=True)}"' if title else ""
    external = destination.startswith(("http://", "https://"))
    relation = ' rel="noopener noreferrer"' if external else ""
    return f'<a href="{html.escape(destination, quote=True)}"{attributes}{relation}>{body}</a>'


def _image(url: str, title: str, alt: str) -> str:
    source = safe_url(url)
    escaped_alt = html.escape(alt, quote=True)
    if source is None:
        return escaped_alt
    attributes = f' title="{html.escape(title, quote=True)}"' if title else ""
    return (
        f'<img src="{html.escape(source, quote=True)}" alt="{escaped_alt}"'
        f'{attributes} loading="lazy">'
    )


def _inline(text: str, ctx: _Context) -> str:
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            out.append(_emphasis(html.escape("".join(pending), quote=False)))
            pending.clear()

    position = 0
    length = len(text)
    while position < length:
        match = _INLINE_SCAN.search(text, position)
        if match is None:
            pending.append(text[position:])
            break
        pending.append(text[position : match.start()])
        kind = match.lastgroup
        position = match.end()

        if kind == "esc":
            # A backslash-escaped char must never reach _emphasis, or "\*" would
            # still open emphasis. Emit it as finished HTML instead.
            flush()
            out.append(html.escape(match.group()[1], quote=False))
        elif kind == "br":
            flush()
            out.append("<br>\n")
        elif kind == "code":
            ticks = match.group()
            end = text.find(ticks, position)
            while end != -1 and text[end : end + len(ticks) + 1] == ticks + "`":
                end = text.find(ticks, end + 1)
            if end == -1:
                pending.append(ticks)
                continue
            body = text[position:end].replace("\n", " ")
            if len(body) > 1 and body.startswith(" ") and body.endswith(" "):
                body = body[1:-1]
            flush()
            out.append(f"<code>{html.escape(body, quote=False)}</code>")
            position = end + len(ticks)
        elif kind == "auto":
            raw = match.group()[1:-1]
            url = raw if ":" in raw else f"mailto:{raw}"
            flush()
            out.append(_anchor(url, "", html.escape(raw, quote=False)))
        elif kind == "url":
            raw = match.group().rstrip(".,;:!?'\"")
            position = match.start() + len(raw)
            url = raw if raw.startswith("http") else f"https://{raw}"
            flush()
            out.append(_anchor(url, "", html.escape(raw, quote=False)))
        else:  # "link" / "image"
            close = _match_bracket(text, position)
            target = (
                _link_target(text, close + 1, text[position:close], ctx) if close != -1 else None
            )
            if target is None:
                pending.append(match.group())
                continue
            url, title, end = target
            label = text[position:close]
            flush()
            if kind == "image":
                out.append(_image(url, title, _strip_markup(label)))
            else:
                out.append(_anchor(url, title, _inline(label, ctx)))
            position = end
    flush()
    return "".join(out)


def _strip_markup(text: str) -> str:
    """Plain text for an ``alt`` attribute: drop the inline markers."""
    return re.sub(r"[*_`~\[\]]", "", text)


def render(text: str) -> str:
    """Render Markdown ``text`` to an HTML fragment (front matter skipped)."""
    _, _, body = split_front_matter(text)
    ctx = _Context()
    lines = _normalize(body)
    _collect_refs(lines, ctx)
    return "\n".join(chunk for _, chunk in _parse(lines, ctx))


# Typography for rendered Markdown. Scoped under .md so it can never leak into
# the surrounding preview chrome.
CSS = """
.md { line-height: 1.65; overflow-wrap: break-word; }
.md > :first-child { margin-top: 0; }
.md h1, .md h2, .md h3, .md h4, .md h5, .md h6 { line-height: 1.25; margin: 1.6em 0 0.6em; }
.md h1 { font-size: 1.75rem; padding-bottom: 0.3em;
  border-bottom: 1px solid color-mix(in srgb, currentColor 15%, transparent); }
.md h2 { font-size: 1.35rem; padding-bottom: 0.3em;
  border-bottom: 1px solid color-mix(in srgb, currentColor 12%, transparent); }
.md h3 { font-size: 1.15rem; }
.md h4, .md h5, .md h6 { font-size: 1rem; }
.md p { margin: 0.9em 0; }
.md a { color: var(--accent); }
.md code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.88em; padding: 0.15em 0.35em; border-radius: 0.3em;
  background: color-mix(in srgb, currentColor 8%, transparent); }
.md pre.code code { padding: 0; background: none; font-size: 1em; }
.md .codewrap { position: relative; }
.md .codewrap::after { content: attr(data-lang); position: absolute; top: 0.45rem; right: 0.6rem;
  font-size: 0.68rem; letter-spacing: 0.04em; text-transform: uppercase; opacity: 0.4; }
.md blockquote { margin: 1em 0; padding: 0.1em 1em; opacity: 0.85;
  border-left: 3px solid color-mix(in srgb, var(--accent) 55%, transparent); }
.md ul, .md ol { padding-left: 1.6em; margin: 0.9em 0; }
.md li { margin: 0.25em 0; }
.md li.task { list-style: none; margin-left: -1.3em; }
.md li.task input { margin-right: 0.45em; }
.md table { border-collapse: collapse; margin: 1em 0; display: block; overflow-x: auto;
  max-width: 100%; }
.md th, .md td { border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
  padding: 0.35rem 0.7rem; }
.md thead th { background: color-mix(in srgb, currentColor 6%, transparent); }
.md hr { border: 0; height: 1px; margin: 2em 0;
  background: color-mix(in srgb, currentColor 18%, transparent); }
.md img { max-width: 100%; height: auto; border-radius: 0.3rem; }
.md del { opacity: 0.7; }
"""
