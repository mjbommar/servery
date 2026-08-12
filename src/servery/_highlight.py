"""Pure-stdlib syntax highlighting for the opt-in ``--preview`` renderer.

There is no lexer library in the standard library, so this module is a small,
deliberately-bounded one. Two strategies:

* **Python** is tokenized by the stdlib's own :mod:`tokenize`, so the result is
  *exactly* CPython's view of the source (f-strings, soft keywords, and all).
  If the file does not tokenize (a syntax error, a truncated read), whatever was
  produced before the failure is kept and the rest renders as plain text.
* **Everything else** goes through a single-pass scanner: one compiled
  alternation per language, matched left-to-right with :meth:`re.Pattern.finditer`.
  Every quantifier in those patterns is *possessive* (``*+``/``++``) or lazy, so
  a pathological file cannot trigger catastrophic backtracking.

The output is HTML for the inside of a ``<pre>``: every character is escaped and
the only markup is ``<span class="…">`` from the fixed :data:`CLASSES` set — so
it is safe under the strict CSP servery applies to its generated pages. No span
ever crosses a newline, which lets :func:`code_block` wrap lines for the
CSS-counter line numbers without repairing markup.
"""

from __future__ import annotations

import builtins
import html
import io
import keyword
import re
import tokenize
from collections.abc import Callable, Iterator, Sequence

# Token CSS classes (kept one character wide — a big file emits a lot of these).
# k keyword · b builtin/type · s string · c comment · n number · d meta/decorator
# t name/tag · e escape/special · i inserted line · x deleted line
CLASSES = ("k", "b", "s", "c", "n", "d", "t", "e", "i", "x")

# A span is (start, end, emitter) over the source text; the emitter is either a
# CSS class or a callable that renders the matched text to HTML itself.
_Emitter = str | Callable[[str], str]
_Span = tuple[int, int, _Emitter]
_Rule = tuple[str, _Emitter]

# --- shared pattern fragments -------------------------------------------

# Possessive quantifiers: the inner "no delimiter, no backslash, no newline" run
# can never be re-tried, so an unterminated string is O(n), not exponential.
_DQ = r'"[^"\\\n]*+(?:\\.[^"\\\n]*+)*+"?'
_SQ = r"'[^'\\\n]*+(?:\\.[^'\\\n]*+)*+'?"
# Same, but the closing quote is REQUIRED: for C-likes a lone ' is an apostrophe
# in a comment or a Rust lifetime, and must not swallow the rest of the line.
_SQ_CLOSED = r"'[^'\\\n]*+(?:\\.[^'\\\n]*+)*+'"
_BACKTICK = r"`[^`\\]*+(?:\\(?s:.)[^`\\]*+)*+`?"
_TRIPLE = r'"""(?s:.)*?(?:"""|\Z)' + "|" + r"'''(?s:.)*?(?:'''|\Z)"
_BLOCK_COMMENT = r"/\*(?s:.)*?(?:\*/|\Z)"
_NUMBER = r"\b(?:0[xXbBoO][0-9a-fA-F_]++|\d[\d_]*+(?:\.\d[\d_]*+)?(?:[eE][+-]?\d++)?)[a-zA-Z_]*+"


def _words(names: str) -> str:
    """A word-bounded alternation over whitespace-separated ``names``."""
    return r"\b(?:" + "|".join(sorted(names.split(), key=len, reverse=True)) + r")\b"


# --- language keyword tables --------------------------------------------

_C_KEYWORDS = """
auto break case const continue default do else enum extern for goto if inline register restrict
return sizeof static struct switch typedef union volatile while _Atomic _Bool _Static_assert
"""
_C_TYPES = "char double float int long short signed unsigned void size_t bool NULL true false"
_CPP_KEYWORDS = (
    _C_KEYWORDS
    + """
class namespace template typename public private protected virtual override final friend using
new delete this nullptr try catch throw constexpr consteval decltype explicit mutable operator
static_cast dynamic_cast const_cast reinterpret_cast co_await co_return co_yield concept requires
"""
)
_JS_KEYWORDS = """
async await break case catch class const continue debugger default delete do else export extends
finally for function get if import in instanceof let new of return set static super switch this
throw try typeof var void while with yield
"""
_JS_BUILTINS = """
Array Boolean Date Error JSON Map Math Number Object Promise RegExp Set String Symbol WeakMap
console document window globalThis undefined null true false NaN Infinity require module exports
"""
_TS_KEYWORDS = (
    _JS_KEYWORDS
    + """
abstract as declare enum implements interface is keyof namespace never public private protected
readonly satisfies type unknown any infer out override
"""
)
_TS_BUILTINS = _JS_BUILTINS + " string number boolean object bigint symbol void"
_JAVA_KEYWORDS = """
abstract assert break case catch class const continue default do else enum extends final finally
for goto if implements import instanceof interface native new package private protected public
return static strictfp super switch synchronized this throw throws transient try var void volatile
while record sealed permits yield
"""
_JAVA_TYPES = "boolean byte char double float int long short String Object true false null"
_CSHARP_KEYWORDS = """
abstract as async await base break case catch checked class const continue default delegate do
else enum event explicit extern finally fixed for foreach get goto if implicit in interface
internal is lock namespace new operator out override params private protected public readonly ref
return sealed set sizeof stackalloc static struct switch this throw try typeof unchecked unsafe
using var virtual void volatile where while yield record init
"""
_CSHARP_TYPES = "bool byte char decimal double float int long object sbyte short string uint ulong ushort true false null"  # noqa: E501
_GO_KEYWORDS = """
break case chan const continue default defer else fallthrough for func go goto if import interface
map package range return select struct switch type var
"""
_GO_TYPES = """
bool byte complex64 complex128 error float32 float64 int int8 int16 int32 int64 rune string uint
uint8 uint16 uint32 uint64 uintptr any true false nil iota make new len cap append copy delete
panic recover print println
"""
_RUST_KEYWORDS = """
as async await break const continue crate dyn else enum extern fn for if impl in let loop match
mod move mut pub ref return self Self static struct super trait type unsafe use where while union
"""
_RUST_TYPES = """
bool char f32 f64 i8 i16 i32 i64 i128 isize str u8 u16 u32 u64 u128 usize String Vec Option Result
Box Some None Ok Err true false
"""
_SWIFT_KEYWORDS = """
associatedtype class deinit enum extension fileprivate func import init inout internal let open
operator private protocol public rethrows static struct subscript typealias var break case
continue default defer do else fallthrough for guard if in repeat return switch where while as
catch is nil super self Self throw throws try async await actor some any
"""
_SWIFT_TYPES = (
    "Any Bool Character Double Float Int String UInt Array Dictionary Set Optional true false"
)
_KOTLIN_KEYWORDS = """
as break by catch class companion const constructor continue crossinline data do else enum
external final finally for fun get if import in infix init inline inner interface internal is
lateinit noinline null object open operator out override package private protected public reified
return sealed set super suspend tailrec this throw try typealias val var vararg when where while
"""
_KOTLIN_TYPES = "Any Array Boolean Byte Char Double Float Int List Long Map Nothing Set Short String Unit true false"  # noqa: E501
_PHP_KEYWORDS = """
abstract and array as break callable case catch class clone const continue declare default do echo
else elseif empty enddeclare endfor endforeach endif endswitch endwhile enum extends final finally
fn for foreach function global goto if implements include include_once instanceof insteadof
interface isset list match namespace new or print private protected public readonly require
require_once return static switch throw trait try unset use var while xor yield
"""
_PHP_BUILTINS = "true false null int float string bool object void mixed self parent this"
_OBJC_KEYWORDS = (
    _C_KEYWORDS
    + """
@interface @implementation @protocol @end @property @synthesize @selector @class self super nil id
BOOL YES NO instancetype
"""
)
_SH_KEYWORDS = """
if then elif else fi for while until do done case esac function select in return break continue
local export readonly declare typeset shift source eval exec trap set unset
"""
_SH_BUILTINS = """
echo printf cd pwd read test true false exit kill wait alias unalias jobs fg bg umask ulimit type
command builtin let mapfile getopts hash times
"""
_SQL_KEYWORDS = """
select from where group by order having limit offset insert into values update set delete create
table view index drop alter add column primary key foreign references unique not null default
join inner left right full outer on as distinct union all exists between like in is and or case
when then else end with recursive returning constraint check cascade begin commit rollback grant
revoke truncate explain analyze
"""
_SQL_TYPES = """
int integer bigint smallint serial bigserial numeric decimal real double precision float char
varchar text bytea boolean date time timestamp timestamptz interval json jsonb uuid array blob
"""
_RUBY_KEYWORDS = """
alias and begin break case class def defined? do else elsif end ensure false for if in module
next nil not or redo rescue retry return self super then true undef unless until when while yield
require require_relative attr_accessor attr_reader attr_writer include extend lambda proc puts
"""
_PERL_KEYWORDS = """
my our local sub if elsif else unless while until for foreach do last next redo return package use
no require BEGIN END qw qq q eval defined undef bless ref wantarray print printf say chomp chop
"""
_LUA_KEYWORDS = """
and break do else elseif end false for function goto if in local nil not or repeat return then
true until while
"""
_LUA_BUILTINS = """
assert collectgarbage dofile error getmetatable ipairs load next pairs pcall print rawequal rawget
rawlen rawset require select setmetatable tonumber tostring type xpcall self string table math io
os coroutine
"""
_R_KEYWORDS = """
if else repeat while function for in next break TRUE FALSE NULL Inf NaN NA NA_integer_
NA_real_ NA_character_ library require return invisible
"""
_JULIA_KEYWORDS = """
baremodule begin break catch const continue do else elseif end export false finally for function
global if import let local macro module mutable primitive quote return struct true try type using
where while abstract
"""
_LISP_KEYWORDS = """
define defun defvar defparameter defmacro defstruct defclass defmethod lambda let let* letrec if
cond case when unless do loop dolist dotimes setq setf progn quote require provide in-package
and or not car cdr cons list append mapcar apply funcall format
"""
_HASKELL_KEYWORDS = """
case class data default deriving do else foreign if import in infix infixl infixr instance let
module newtype of then type where forall
"""
_DOCKER_INSTRUCTIONS = """
FROM RUN CMD LABEL MAINTAINER EXPOSE ENV ADD COPY ENTRYPOINT VOLUME USER WORKDIR ARG ONBUILD
STOPSIGNAL HEALTHCHECK SHELL AS
"""
_YAML_CONSTANTS = "true false null yes no on off True False Null TRUE FALSE NULL ~"
_TOML_CONSTANTS = "true false inf nan"
_CSS_ATRULES = (
    "media import charset keyframes supports font-face namespace page layer container property"
)


# --- rule builders ------------------------------------------------------


def _c_family(kw: str, types: str, *, preproc: bool = False, template: bool = False) -> list[_Rule]:
    """Rules for a ``/* */`` + ``//`` language with double- and single-quoted literals."""
    rules: list[_Rule] = [(_BLOCK_COMMENT, "c"), (r"//[^\n]*", "c")]
    if preproc:
        rules.append((r"^[ \t]*#[ \t]*[a-z_]++", "d"))
    rules.append((_DQ, "s"))
    if template:
        rules.append((_BACKTICK, "s"))
    rules.append((_SQ_CLOSED, "s"))
    rules.append((_words(kw), "k"))
    rules.append((_words(types), "b"))
    rules.append((_NUMBER, "n"))
    return rules


def _hash_family(kw: str, builtin: str = "", *, triple: bool = False) -> list[_Rule]:
    """Rules for a ``#``-comment scripting language."""
    rules: list[_Rule] = [(r"#[^\n]*", "c")]
    if triple:
        rules.append((_TRIPLE, "s"))
    rules += [(_DQ, "s"), (_SQ, "s"), (_words(kw), "k")]
    if builtin:
        rules.append((_words(builtin), "b"))
    rules.append((_NUMBER, "n"))
    return rules


# Cache key for the nested markup-tag scanner; not a language, so it can never
# collide with a user-supplied ``?lang=`` or an extension mapping.
_TAG_KEY = "\x00tag"


def _markup_tag(text: str) -> str:
    """Sub-highlight one ``<tag …>``: element name, attribute names, values."""
    compiled = _compiled.get(_TAG_KEY)
    if compiled is None:
        compiled = _compiled[_TAG_KEY] = _compile(_MARKUP_TAG_RULES)
    pattern, emitters = compiled
    return _emit(text, _matches(text, pattern, emitters))


_MARKUP_TAG_RULES: tuple[_Rule, ...] = (
    (r"</?[\w:.-]++", "t"),
    (r"/?>", "t"),
    (_DQ, "s"),
    (_SQ, "s"),
    (r"[\w:.-]++(?=\s*=)", "b"),
)

_LANGUAGE_RULES: dict[str, tuple[_Rule, ...]] = {
    "c": tuple(_c_family(_C_KEYWORDS, _C_TYPES, preproc=True)),
    "cpp": tuple(_c_family(_CPP_KEYWORDS, _C_TYPES, preproc=True)),
    "objc": tuple(_c_family(_OBJC_KEYWORDS, _C_TYPES, preproc=True)),
    "javascript": tuple(_c_family(_JS_KEYWORDS, _JS_BUILTINS, template=True)),
    "typescript": tuple(_c_family(_TS_KEYWORDS, _TS_BUILTINS, template=True)),
    "java": tuple(_c_family(_JAVA_KEYWORDS, _JAVA_TYPES)),
    "csharp": tuple(_c_family(_CSHARP_KEYWORDS, _CSHARP_TYPES)),
    "go": tuple(_c_family(_GO_KEYWORDS, _GO_TYPES, template=True)),
    "rust": tuple(_c_family(_RUST_KEYWORDS, _RUST_TYPES)),
    "swift": tuple(_c_family(_SWIFT_KEYWORDS, _SWIFT_TYPES)),
    "kotlin": tuple(_c_family(_KOTLIN_KEYWORDS, _KOTLIN_TYPES)),
    "php": tuple(_c_family(_PHP_KEYWORDS, _PHP_BUILTINS, preproc=False)),
    "shell": tuple(_hash_family(_SH_KEYWORDS, _SH_BUILTINS)),
    "ruby": tuple(_hash_family(_RUBY_KEYWORDS, "", triple=False)),
    "perl": tuple(_hash_family(_PERL_KEYWORDS)),
    "r": tuple(_hash_family(_R_KEYWORDS)),
    "julia": tuple(_hash_family(_JULIA_KEYWORDS, "", triple=True)),
    "lua": (
        (r"--\[\[(?s:.)*?(?:\]\]|\Z)", "c"),
        (r"--[^\n]*", "c"),
        (r"\[\[(?s:.)*?(?:\]\]|\Z)", "s"),
        (_DQ, "s"),
        (_SQ, "s"),
        (_words(_LUA_KEYWORDS), "k"),
        (_words(_LUA_BUILTINS), "b"),
        (_NUMBER, "n"),
    ),
    "haskell": (
        (r"\{-(?s:.)*?(?:-\}|\Z)", "c"),
        (r"--[^\n]*", "c"),
        (_DQ, "s"),
        (_words(_HASKELL_KEYWORDS), "k"),
        (_NUMBER, "n"),
    ),
    "sql": (
        (_BLOCK_COMMENT, "c"),
        (r"--[^\n]*", "c"),
        (_SQ, "s"),
        (_DQ, "b"),  # a quoted identifier, not a string, in standard SQL
        (_words(_SQL_KEYWORDS.upper()), "k"),
        (_words(_SQL_KEYWORDS), "k"),
        (_words(_SQL_TYPES.upper()), "b"),
        (_words(_SQL_TYPES), "b"),
        (_NUMBER, "n"),
    ),
    "lisp": (
        (r";[^\n]*", "c"),
        (_DQ, "s"),
        (r"#\\(?:\w++|.)", "e"),
        (_words(_LISP_KEYWORDS), "k"),
        (_NUMBER, "n"),
    ),
    "json": (
        (r'"[^"\\\n]*+(?:\\.[^"\\\n]*+)*+"(?=[ \t]*+:)', "t"),
        (_DQ, "s"),
        (r"\b(?:true|false|null)\b", "k"),
        (_NUMBER, "n"),
    ),
    "yaml": (
        (r"#[^\n]*", "c"),
        (r"^---[ \t]*$|^\.\.\.[ \t]*$", "e"),
        (r"^[ \t]*+(?:-[ \t]++)*+[\w.\"'/@-]++(?=[ \t]*+:(?:[ \t]|$))", "t"),
        (r"[&*][\w.-]++", "d"),
        (r"![\w!/.-]++", "d"),
        (_DQ, "s"),
        (_SQ, "s"),
        (_words(_YAML_CONSTANTS), "k"),
        (_NUMBER, "n"),
    ),
    "ini": (
        (r"[#;][^\n]*", "c"),
        (r"^[ \t]*\[[^\]\n]*+\]", "t"),
        (r"^[ \t]*+[\w.\"'-]++(?=[ \t]*+=)", "b"),
        (_TRIPLE, "s"),
        (_DQ, "s"),
        (_SQ, "s"),
        (_words(_TOML_CONSTANTS), "k"),
        (r"\d{4}-\d{2}-\d{2}(?:[T ][\d:.+Z-]++)?", "n"),
        (_NUMBER, "n"),
    ),
    "css": (
        (_BLOCK_COMMENT, "c"),
        (r"@(?:" + "|".join(_CSS_ATRULES.split()) + r")\b", "k"),
        (r"#[0-9a-fA-F]{3,8}\b", "n"),
        (_DQ, "s"),
        (_SQ, "s"),
        (r"[-\w]++(?=[ \t]*+:)", "b"),
        (r"[.#][-\w]++|::?[-\w]++|\[[^\]\n]*+\]", "t"),
        (r"\b\d[\d.]*+(?:px|em|rem|ex|ch|vw|vh|vmin|vmax|%|s|ms|deg|fr|pt|cm|mm|in)?\b", "n"),
    ),
    "markup": (
        (r"<!--(?s:.)*?(?:-->|\Z)", "c"),
        (r"<![^>\n]*+>", "d"),
        (r"<\?[\s\S]*?\?>", "d"),
        (r"</?[\w:.-]++(?:[^<>\"']|" + _DQ + "|" + _SQ + r")*+/?>", _markup_tag),
        (r"&#?\w++;", "e"),
    ),
    "latex": (
        (r"(?<!\\)%[^\n]*", "c"),
        (r"\\(?:begin|end)\{[^}\n]*+\}", "t"),
        (r"\\[a-zA-Z@]++\*?", "k"),
        (r"\\[^a-zA-Z\s]", "e"),
        (r"\$\$(?s:.)*?(?:\$\$|\Z)", "s"),
        (r"\$[^$\n]*+\$", "s"),
        (r"[{}]", "e"),
        (r"\b\d[\d.]*+(?:pt|cm|mm|in|em|ex|bp|dd|sp)?\b", "n"),
    ),
    "bibtex": (
        (r"@\w++", "k"),
        (r"^[ \t]*+[\w-]++(?=[ \t]*+=)", "b"),
        (_DQ, "s"),
        (r"\{[^{}\n]*+\}", "s"),
        (_NUMBER, "n"),
    ),
    "markdown": (
        (r"^ {0,3}(?:```|~~~)[^\n]*", "e"),
        (r"^ {0,3}#{1,6}[^\n]*", "k"),
        (r"^ {0,3}(?:[-*_][ \t]*){3,}$", "e"),
        (r"^ {0,3}(?:=+|-+)[ \t]*$", "k"),
        (r"^ {0,3}>+", "b"),
        (r"^[ \t]*(?:[-*+]|\d{1,9}[.)])(?=[ \t])", "b"),
        (r"`[^`\n]++`", "s"),
        (r"!?\[[^\]\n]*+\]\([^)\n]*+\)", "t"),
        (r"<(?:https?|mailto):[^>\s]++>", "t"),
        (r"\*\*[^\n]*?\*\*|__[^\n]*?__", "d"),
        (r"^\[[^\]\n]++\]:[^\n]*", "t"),
    ),
    "rst": (
        (r"^\.\.[ \t]+[\w-]++::[^\n]*", "k"),
        (r"^\.\.[^\n]*", "c"),
        (r"^[=\-~`^\"'*+#_:.]{3,}[ \t]*$", "k"),
        (r"^:[^:\n]++:", "b"),
        (r"``[^`\n]++``", "s"),
        (r":[\w:+.-]++:`[^`\n]*+`", "t"),
        (r"`[^`\n]++`_?", "t"),
        (r"\*\*[^*\n]++\*\*", "d"),
    ),
    "diff": (
        (r"^(?:diff|index|new file|deleted file|similarity|rename)[^\n]*", "d"),
        (r"^(?:\+\+\+|---)[^\n]*", "d"),
        (r"^@@[^\n]*", "t"),
        (r"^\+[^\n]*", "i"),
        (r"^-[^\n]*", "x"),
    ),
    "makefile": (
        (r"#[^\n]*", "c"),
        (r"^\.[A-Z]++\b", "k"),
        (r"^[\w%./$()-]++[ \t]*+:(?!=)", "t"),
        (r"\$[({][^)}\n]*+[)}]|\$[@<^?*%]", "b"),
        (_DQ, "s"),
        (_SQ, "s"),
    ),
    "dockerfile": (
        (r"#[^\n]*", "c"),
        (r"^[ \t]*+(?:" + "|".join(_DOCKER_INSTRUCTIONS.split()) + r")\b", "k"),
        (r"(?i:^[ \t]*+(?:" + "|".join(_DOCKER_INSTRUCTIONS.split()) + r")\b)", "k"),
        (_DQ, "s"),
        (_SQ, "s"),
        (r"\$[{\w][^\s\"']*+", "b"),
    ),
}

# Human-readable names for the preview page's language badge.
LANGUAGE_NAMES: dict[str, str] = {
    "bibtex": "BibTeX",
    "c": "C",
    "cpp": "C++",
    "csharp": "C#",
    "css": "CSS",
    "diff": "Diff",
    "dockerfile": "Dockerfile",
    "go": "Go",
    "haskell": "Haskell",
    "ini": "INI/TOML",
    "java": "Java",
    "javascript": "JavaScript",
    "json": "JSON",
    "julia": "Julia",
    "kotlin": "Kotlin",
    "latex": "LaTeX",
    "lisp": "Lisp",
    "lua": "Lua",
    "makefile": "Makefile",
    "markdown": "Markdown",
    "markup": "HTML/XML",
    "objc": "Objective-C",
    "perl": "Perl",
    "php": "PHP",
    "python": "Python",
    "r": "R",
    "rst": "reStructuredText",
    "ruby": "Ruby",
    "rust": "Rust",
    "shell": "Shell",
    "sql": "SQL",
    "swift": "Swift",
    "text": "Text",
    "typescript": "TypeScript",
    "yaml": "YAML",
}

# Extension (lowercase, no dot) -> language id.
_EXT_LANGUAGE: dict[str, str] = {
    "bash": "shell",
    "bat": "shell",
    "bib": "bibtex",
    "c": "c",
    "cc": "cpp",
    "cfg": "ini",
    "clj": "lisp",
    "cls": "latex",
    "cmake": "makefile",
    "conf": "ini",
    "cpp": "cpp",
    "cs": "csharp",
    "css": "css",
    "cxx": "cpp",
    "diff": "diff",
    "el": "lisp",
    "go": "go",
    "h": "c",
    "hpp": "cpp",
    "hs": "haskell",
    "htm": "markup",
    "html": "markup",
    "hxx": "cpp",
    "ini": "ini",
    "java": "java",
    "jl": "julia",
    "js": "javascript",
    "json": "json",
    "jsonc": "json",
    "jsx": "javascript",
    "kt": "kotlin",
    "kts": "kotlin",
    "less": "css",
    "lisp": "lisp",
    "lua": "lua",
    "m": "objc",
    "markdown": "markdown",
    "md": "markdown",
    "mjs": "javascript",
    "mk": "makefile",
    "mkd": "markdown",
    "patch": "diff",
    "php": "php",
    "pl": "perl",
    "pm": "perl",
    "py": "python",
    "pyi": "python",
    "pyw": "python",
    "r": "r",
    "rb": "ruby",
    "rs": "rust",
    "rst": "rst",
    "scm": "lisp",
    "scss": "css",
    "sh": "shell",
    "sql": "sql",
    "sty": "latex",
    "svg": "markup",
    "swift": "swift",
    "tex": "latex",
    "toml": "ini",
    "ts": "typescript",
    "tsx": "typescript",
    "txt": "text",
    "vue": "markup",
    "xhtml": "markup",
    "xml": "markup",
    "xsl": "markup",
    "yaml": "yaml",
    "yml": "yaml",
    "zsh": "shell",
}

# Extension-less (or fixed) filenames worth recognizing by name.
_NAME_LANGUAGE: dict[str, str] = {
    ".bash_profile": "shell",
    ".bashrc": "shell",
    ".editorconfig": "ini",
    ".gitconfig": "ini",
    ".profile": "shell",
    ".zshrc": "shell",
    "cmakelists.txt": "makefile",
    "containerfile": "dockerfile",
    "dockerfile": "dockerfile",
    "gnumakefile": "makefile",
    "makefile": "makefile",
    "pkgbuild": "shell",
}


def language_for(filename: str) -> str | None:
    """Best-guess language id for ``filename``, or ``None`` when unknown.

    Pure name inspection — the file is never opened.
    """
    base = filename.rsplit("/", 1)[-1].lower()
    named = _NAME_LANGUAGE.get(base)
    if named is not None:
        return named
    if base.startswith("dockerfile.") or base.endswith(".dockerfile"):
        return "dockerfile"
    head, dot, ext = base.rpartition(".")
    if not dot or not head:
        return None
    return _EXT_LANGUAGE.get(ext)


def supported(language: str | None) -> bool:
    """True when :func:`highlight` has real rules for ``language``."""
    return language == "python" or language in _LANGUAGE_RULES


# Fenced-code info strings people actually write, mapped to our language ids.
# Anything not here falls back to the extension table ("py", "js", "sh", …).
_INFO_ALIASES: dict[str, str] = {
    "bash": "shell",
    "c#": "csharp",
    "c++": "cpp",
    "console": "shell",
    "golang": "go",
    "htm": "markup",
    "html": "markup",
    "jinja": "markup",
    "plain": "text",
    "plaintext": "text",
    "sh": "shell",
    "shell-session": "shell",
    "svg": "markup",
    "tex": "latex",
    "xml": "markup",
    "zsh": "shell",
}


def language_for_info(info: str) -> str | None:
    """Resolve a fenced-code info string (```` ```python ````) to a language id."""
    token = info.strip().split(None, 1)[0] if info.strip() else ""
    token = token.strip("{}").lstrip(".").lower()
    if not token:
        return None
    if token in LANGUAGE_NAMES:
        return token
    alias = _INFO_ALIASES.get(token)
    if alias is not None:
        return alias
    return _EXT_LANGUAGE.get(token)


# --- the scanner --------------------------------------------------------

_compiled: dict[str, tuple[re.Pattern[str], tuple[_Emitter, ...]]] = {}


def _compile(rules: Sequence[_Rule]) -> tuple[re.Pattern[str], tuple[_Emitter, ...]]:
    """Fuse ``rules`` into one alternation; group N maps to emitter N."""
    parts = [f"(?P<g{i}>{pattern})" for i, (pattern, _) in enumerate(rules)]
    pattern = re.compile("|".join(parts), re.MULTILINE)
    return pattern, tuple(emitter for _, emitter in rules)


def _rules_for(language: str) -> tuple[re.Pattern[str], tuple[_Emitter, ...]] | None:
    cached = _compiled.get(language)
    if cached is not None:
        return cached
    rules = _LANGUAGE_RULES.get(language)
    if rules is None:
        return None
    # Benign race under free threading: two threads may compile the same pattern;
    # both results are equivalent and the dict assignment is atomic.
    built = _compile(rules)
    _compiled[language] = built
    return built


def _matches(
    text: str, pattern: re.Pattern[str], emitters: tuple[_Emitter, ...]
) -> Iterator[_Span]:
    for match in pattern.finditer(text):
        index = int(match.lastgroup[1:]) if match.lastgroup else 0  # "gN" -> N
        yield match.start(), match.end(), emitters[index]


# --- Python, via the stdlib tokenizer -----------------------------------

_PY_BUILTINS = frozenset(name for name in dir(builtins) if not name.startswith("_"))
_PY_DEFINERS = frozenset(("def", "class"))


def _line_offsets(text: str) -> list[int]:
    """Absolute offset of the start of each 1-indexed source line."""
    offsets = [0, 0]
    start = 0
    while (index := text.find("\n", start)) != -1:
        start = index + 1
        offsets.append(start)
    return offsets


def _python_spans(text: str) -> list[_Span]:
    """Tokenize ``text`` with :mod:`tokenize`, keeping whatever parses.

    ``tokenize`` raises on a truncated or invalid file *part way through*; the
    spans produced up to that point are still correct, so we keep them and let
    the tail render as plain text rather than losing the whole highlight.
    """
    offsets = _line_offsets(text)
    spans: list[_Span] = []
    definer = False
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            kind = token.type
            cls: str | None = None
            if kind == tokenize.COMMENT:
                cls = "c"
            elif kind == tokenize.STRING or _is_fstring_part(kind):
                cls = "s"
            elif kind == tokenize.NUMBER:
                cls = "n"
            elif kind == tokenize.NAME:
                name = token.string
                if definer:
                    cls, definer = "t", False
                elif keyword.iskeyword(name):
                    cls = "k"
                    definer = name in _PY_DEFINERS
                elif name in _PY_BUILTINS or keyword.issoftkeyword(name):
                    cls = "b"
            elif kind == tokenize.OP and token.string == "@" and token.start[1] == 0:
                cls = "d"
            if cls is None:
                continue
            srow, scol = token.start
            erow, ecol = token.end
            if erow >= len(offsets):  # pragma: no cover - defensive
                break
            spans.append((offsets[srow] + scol, offsets[erow] + ecol, cls))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        pass  # keep the prefix we already tokenized
    return spans


def _is_fstring_part(kind: int) -> bool:
    """True for the 3.12+ f-string token types (absent on older tokenizers)."""
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        value = getattr(tokenize, name, None)
        if value is not None and kind == value:
            return True
    return False


# --- rendering ----------------------------------------------------------


def _span_html(chunk: str, cls: str) -> str:
    """Wrap ``chunk`` in a token span, split so no span crosses a newline."""
    if "\n" not in chunk:
        return f'<span class="{cls}">{html.escape(chunk, quote=False)}</span>'
    return "\n".join(
        f'<span class="{cls}">{html.escape(line, quote=False)}</span>' if line else ""
        for line in chunk.split("\n")
    )


def _emit(text: str, spans: Iterator[_Span] | list[_Span]) -> str:
    out: list[str] = []
    position = 0
    for start, end, emitter in spans:
        if start < position:  # overlapping match (shouldn't happen) - skip
            continue
        if start > position:
            out.append(html.escape(text[position:start], quote=False))
        chunk = text[start:end]
        # A str emitter is a CSS class; anything else renders the chunk itself
        # (the nested markup-tag scanner). isinstance, not callable(), so the
        # type checker can narrow the union.
        out.append(_span_html(chunk, emitter) if isinstance(emitter, str) else emitter(chunk))
        position = end
    out.append(html.escape(text[position:], quote=False))
    return "".join(out)


def highlight(text: str, language: str | None) -> str:
    """Render ``text`` as escaped HTML with token spans (no wrapping element).

    An unknown or unsupported ``language`` degrades to plain escaped text, so a
    caller never has to special-case it.
    """
    if language == "python":
        spans = _python_spans(text)
        if spans:
            return _emit(text, spans)
        return html.escape(text, quote=False)
    compiled = _rules_for(language) if language else None
    if compiled is None:
        return html.escape(text, quote=False)
    pattern, emitters = compiled
    return _emit(text, _matches(text, pattern, emitters))


def code_block(text: str, language: str | None, *, line_numbers: bool = True) -> str:
    """A complete ``<pre>`` block: highlighted, optionally with line numbers.

    Line numbers come from a CSS counter on each line's ``<span class="l">``, so
    the markup stays script-free and selecting the block copies the code without
    the gutter.
    """
    body = highlight(text.rstrip("\n"), language)
    classes = "code" + (" ln" if line_numbers else "")
    if not line_numbers:
        return f'<pre class="{classes}"><code>{body}</code></pre>'
    lines = "".join(f'<span class="l">{line}\n</span>' for line in body.split("\n"))
    return f'<pre class="{classes}"><code>{lines}</code></pre>'


# Token colors, tuned for both light and dark via light-dark() so a single
# declaration covers each theme (the preview page sets color-scheme).
CSS = """
pre.code { overflow-x: auto; padding: 0.85rem 1rem; border-radius: 0.5rem; margin: 0.8rem 0;
  background: color-mix(in srgb, currentColor 5%, transparent); font-size: 0.86rem;
  line-height: 1.5; tab-size: 4; }
pre.code code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
pre.code.ln { counter-reset: l; padding-left: 0.5rem; }
pre.code.ln .l { counter-increment: l; display: block; }
pre.code.ln .l::before { content: counter(l); display: inline-block; width: 3ch;
  margin-right: 1.2ch; text-align: right; opacity: 0.35; user-select: none;
  -webkit-user-select: none; }
.k { color: light-dark(#7c3aed, #c4b5fd); font-weight: 600; }
.b { color: light-dark(#0369a1, #7dd3fc); }
.s { color: light-dark(#15803d, #86efac); }
.c { color: light-dark(#6b7280, #9ca3af); font-style: italic; }
.n { color: light-dark(#b45309, #fcd34d); }
.d { color: light-dark(#be123c, #fda4af); }
.t { color: light-dark(#1d4ed8, #93c5fd); }
.e { color: light-dark(#a16207, #fbbf24); }
.i { color: light-dark(#15803d, #86efac); }
.x { color: light-dark(#b91c1c, #fca5a5); }
"""
