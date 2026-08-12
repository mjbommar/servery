# Preview & metadata

Two **opt-in** features that let a share be *read* and *searched*, not just
downloaded. Both are off by default, cost nothing when off, and — like everything
else in servery — are pure standard library with no JavaScript.

```bash
servery ./docs --preview --metadata
```

## Previewing a file

With `--preview`, appending `?preview=1` to any file URL renders a page *around*
the file instead of sending its bytes:

| File type | What you get |
| --- | --- |
| Markdown (`.md`) | rendered to HTML — headings, lists, tables, links, highlighted code fences |
| Source code | syntax-highlighted with line numbers (Python via the stdlib's own `tokenize`) |
| Jupyter (`.ipynb`) | markdown cells rendered, code cells highlighted, text outputs shown |
| JSON | re-indented and highlighted |
| CSV / TSV | a real table (first 500 rows) |
| Images | shown inline |
| Audio / video | native `<audio>` / `<video>` controls |
| PDF, archives, binaries | an honest card with **Download** / **Open raw** |

Each listing row grows a **🔍** link for anything previewable, next to the existing
**↓** download link.

### Rendered or source

Anything textual can also be read as highlighted source:

```text
http://localhost:8000/README.md?preview=1        # rendered
http://localhost:8000/README.md?preview=source   # syntax-highlighted source
```

The page carries a Rendered/Source toggle, so it is one click either way.

### Languages

Python is tokenized by the standard library's own `tokenize`, so the result is
exactly CPython's view of the source. Roughly 35 more languages go through a
bounded scanner: C, C++, C#, Java, Go, Rust, Swift, Kotlin, Objective-C,
JavaScript/TypeScript, PHP, Ruby, Perl, Lua, R, Julia, Haskell, Lisp, shell, SQL,
HTML/XML/SVG, CSS, LaTeX, BibTeX, YAML, TOML/INI, JSON, Markdown,
reStructuredText, diff, Makefile, and Dockerfile.

### Limits, on purpose

- Files above `--preview-max-bytes` (default **2 MiB**) are not read; you get a
  download card instead. The preview holds the whole file in memory.
- The Markdown renderer is a **subset**, not CommonMark — see
  [Principles](../PRINCIPLES.md). It covers what a README needs; it is not GFM.
- **Raw HTML in Markdown is escaped, never passed through**, and link/image URLs
  must pass a scheme allowlist, so a `javascript:` or `data:` URL cannot survive.
  A hostile `.md` someone uploaded to your drop box cannot script the preview page.
- The preview page's Content-Security-Policy is the listing's, plus
  `media-src 'self'` for the players. `img-src` stays `'self'`, so **remote images
  (README badges, for example) do not load** — a preview never fetches from a
  third party on your behalf.

## Metadata

With `--metadata`, servery reads *inside* each file — bounded, and never executing
anything — and normalizes what it finds onto one small record:

| Format | Extracted |
| --- | --- |
| Markdown | YAML/TOML front matter; else the first heading and paragraph |
| Python | module docstring, `__author__`, `__version__`, `__date__`, class/function counts (via `ast`) |
| LaTeX | `\title{}`, `\author{}`, `\date{}`, `\documentclass{}` |
| HTML | `<title>`, `<meta name=author/description/keywords>`, `<html lang>` |
| TOML / JSON | PEP 621 `[project]`, Cargo `[package]`, `package.json` fields |
| Notebooks | first markdown heading, kernel, language, cell count |
| CSV / TSV | column names and row count |
| reStructuredText | title and `:Field:` docinfo |
| Images | dimensions (PNG, JPEG, GIF, WebP, BMP, SVG) + PNG text chunks |
| MP3 | ID3v2 title, artist, album, year, genre |
| PDF | Info dictionary title/author/subject/keywords, PDF version |

### Show

The listing gains a **Title** column (author or description underneath), and the
column header sorts by it:

| Query | Effect |
| --- | --- |
| `?C=T` | sort by extracted title |
| `?C=A` | sort by extracted author |

Files with nothing extracted sort last, not first.

### Filter

| Query | Effect |
| --- | --- |
| `?meta=author:lovelace` | files whose extracted author contains `lovelace` |
| `?meta=tag:draft` | files tagged `draft` (also: clickable tag chips) |
| `?meta=title:report` | by title — likewise `description`, `date`, `version`, `lang` |
| `?meta=lovelace` | no field given: search every extracted field |

Directories are always kept, so navigation still works while a filter is on.

### Extract

`?metadata=1` returns JSON instead of a page — for one file, or for a whole
directory:

```console
$ curl -s 'http://localhost:8000/post.md?metadata=1' | jq .metadata
{
  "kind": "markdown",
  "title": "The Title",
  "author": "Ada Lovelace",
  "tags": ["alpha", "beta"]
}

$ curl -s 'http://localhost:8000/?metadata=1' | jq -r '.entries[] | "\(.name)\t\(.metadata.title // "-")"'
sub     -
post.md The Title
```

Each entry carries `name`, `path`, `type`, `size`, `modified`, `content_type`, and
the extracted `metadata`.

### Cost

Extraction reads at most `--metadata-max-bytes` (default **64 KiB**) per file and
caches the result on `(path, mtime, size)`, so re-sorting or re-filtering a large
directory costs no I/O. A corrupt, truncated, or hostile file yields an empty
record — it can never break a listing.

## Flags

| Flag | Default | Description |
| --- | --- | --- |
| `--preview` | off | enable `?preview=` render pages |
| `--preview-max-bytes BYTES` | 2 MiB | largest file the preview will read |
| `--metadata` | off | extract metadata; adds the column, filters, sorts, and `?metadata=1` |
| `--metadata-max-bytes BYTES` | 64 KiB | per-file read budget for extraction |

## Caveats

Like sorting, filtering, and archive download, these are **HTTP/1.1** features. The
buffered HTTP/2 and HTTP/3 backends serve plain files and listings, and ignore
these query parameters.

## See also

- [Serving files](serving.md) — the listing, downloads, archives
- [Principles](../PRINCIPLES.md) — why the Markdown renderer is a subset
