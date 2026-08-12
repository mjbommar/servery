"""Syntax-highlighter tests: language detection, token markup, and safety."""

import re
import time
import unittest

from servery import _highlight

# Every <span> the highlighter emits, for the "no span crosses a newline" check.
_SPAN_RE = re.compile(r'<span class="(\w+)">(.*?)</span>', re.DOTALL)


class LanguageDetectionTest(unittest.TestCase):
    def test_by_extension(self):
        self.assertEqual(_highlight.language_for("a/b/main.py"), "python")
        self.assertEqual(_highlight.language_for("style.CSS"), "css")
        self.assertEqual(_highlight.language_for("paper.tex"), "latex")
        self.assertEqual(_highlight.language_for("index.html"), "markup")
        self.assertEqual(_highlight.language_for("notes.md"), "markdown")

    def test_by_filename(self):
        self.assertEqual(_highlight.language_for("Makefile"), "makefile")
        self.assertEqual(_highlight.language_for("Dockerfile"), "dockerfile")
        self.assertEqual(_highlight.language_for("srv/Dockerfile.dev"), "dockerfile")
        self.assertEqual(_highlight.language_for(".bashrc"), "shell")

    def test_unknown_and_dotfiles(self):
        self.assertIsNone(_highlight.language_for("LICENSE"))
        self.assertIsNone(_highlight.language_for("archive.unknownext"))

    def test_info_string_aliases(self):
        self.assertEqual(_highlight.language_for_info("py"), "python")
        self.assertEqual(_highlight.language_for_info("Bash"), "shell")
        self.assertEqual(_highlight.language_for_info("c++"), "cpp")
        self.assertEqual(_highlight.language_for_info("html"), "markup")
        self.assertEqual(_highlight.language_for_info("python title=x"), "python")
        self.assertIsNone(_highlight.language_for_info(""))
        self.assertIsNone(_highlight.language_for_info("no-such-language"))

    def test_supported(self):
        self.assertTrue(_highlight.supported("python"))
        self.assertTrue(_highlight.supported("rust"))
        self.assertFalse(_highlight.supported(None))
        self.assertFalse(_highlight.supported("klingon"))

    def test_every_named_language_has_rules(self):
        for language in _highlight.LANGUAGE_NAMES:
            if language == "text":
                continue
            self.assertTrue(_highlight.supported(language), language)


class PythonHighlightTest(unittest.TestCase):
    def test_uses_the_real_tokenizer(self):
        out = _highlight.highlight("def f(x):\n    return len(x)  # note\n", "python")
        self.assertIn('<span class="k">def</span>', out)
        self.assertIn('<span class="t">f</span>', out)  # the definition name
        self.assertIn('<span class="b">len</span>', out)  # a builtin
        self.assertIn('<span class="c"># note</span>', out)

    def test_strings_and_numbers(self):
        out = _highlight.highlight('x = "hi" + 42\n', "python")
        self.assertIn('<span class="s">"hi"</span>', out)
        self.assertIn('<span class="n">42</span>', out)

    def test_decorator_at_line_start(self):
        out = _highlight.highlight("@property\ndef f(): pass\n", "python")
        self.assertIn('<span class="d">@</span>', out)

    def test_unparseable_python_still_renders(self):
        # The tokenizer raises part way through; the prefix is kept and the rest
        # degrades to escaped text rather than blowing up.
        out = _highlight.highlight("x = 1\ndef (((\n", "python")
        self.assertIn('<span class="n">1</span>', out)
        self.assertIn("def", out)

    def test_unparseable_text_is_still_escaped(self):
        out = _highlight.highlight("!!! <not python> &\n", "python")
        self.assertNotIn("<not", out)
        self.assertIn("&lt;", out)
        self.assertIn("&amp;", out)

    def test_empty_source(self):
        self.assertEqual(_highlight.highlight("", "python"), "")


class GenericLexerTest(unittest.TestCase):
    def test_c_family(self):
        out = _highlight.highlight("int x = 1; // c\n/* block */\n", "c")
        self.assertIn('<span class="b">int</span>', out)
        self.assertIn('<span class="c">// c</span>', out)
        self.assertIn('<span class="c">/* block */</span>', out)

    def test_json_keys_differ_from_values(self):
        out = _highlight.highlight('{"a": "b", "c": true}', "json")
        self.assertIn('<span class="t">"a"</span>', out)
        self.assertIn('<span class="s">"b"</span>', out)
        self.assertIn('<span class="k">true</span>', out)

    def test_markup_sub_highlights_tags(self):
        out = _highlight.highlight('<a href="x">t</a>', "markup")
        self.assertIn('<span class="t">&lt;a</span>', out)
        self.assertIn('<span class="b">href</span>', out)
        self.assertIn('<span class="s">"x"</span>', out)

    def test_latex(self):
        out = _highlight.highlight("\\title{Hi} % note\n$x^2$\n", "latex")
        self.assertIn('<span class="k">\\title</span>', out)
        self.assertIn('<span class="c">% note</span>', out)
        self.assertIn('<span class="s">$x^2$</span>', out)

    def test_diff_marks_insertions_and_deletions(self):
        out = _highlight.highlight("+added\n-removed\n", "diff")
        self.assertIn('<span class="i">+added</span>', out)
        self.assertIn('<span class="x">-removed</span>', out)

    def test_yaml_keys(self):
        out = _highlight.highlight("key: value # c\n", "yaml")
        self.assertIn('<span class="t">key</span>', out)
        self.assertIn('<span class="c"># c</span>', out)

    def test_sql_is_case_insensitive_on_keywords(self):
        for text in ("SELECT * FROM t", "select * from t"):
            out = _highlight.highlight(text, "sql")
            self.assertIn('class="k"', out)

    def test_shell_comment_wins_over_apostrophe(self):
        out = _highlight.highlight("echo hi # don't stop\n", "shell")
        self.assertIn('<span class="c"># don&#x27;t stop</span>', out.replace("'", "&#x27;"))


class OutputSafetyTest(unittest.TestCase):
    def test_html_in_source_is_escaped(self):
        out = _highlight.highlight('<script>alert("x")</script>', "javascript")
        self.assertNotIn("<script", out)
        self.assertIn("&lt;script&gt;", out)

    def test_unknown_language_is_plain_escaped_text(self):
        out = _highlight.highlight("a < b & c", None)
        self.assertEqual(out, "a &lt; b &amp; c")

    def test_no_span_crosses_a_newline(self):
        source = 'a = """multi\nline\nstring"""\n# tail\n'
        out = _highlight.highlight(source, "python")
        for _, body in _SPAN_RE.findall(out):
            self.assertNotIn("\n", body)

    def test_block_comment_spanning_lines_is_split(self):
        out = _highlight.highlight("/* one\ntwo */\nx", "c")
        for _, body in _SPAN_RE.findall(out):
            self.assertNotIn("\n", body)


class CodeBlockTest(unittest.TestCase):
    def test_line_numbers_use_a_css_counter(self):
        out = _highlight.code_block("a\nb\nc", "python")
        self.assertEqual(out.count('<span class="l">'), 3)
        self.assertIn('class="code ln"', out)

    def test_line_numbers_can_be_disabled(self):
        out = _highlight.code_block("a\nb", "python", line_numbers=False)
        self.assertNotIn('<span class="l">', out)
        self.assertIn('<pre class="code">', out)

    def test_trailing_newlines_do_not_add_blank_lines(self):
        out = _highlight.code_block("a\nb\n\n\n", "python")
        self.assertEqual(out.count('<span class="l">'), 2)

    def test_css_defines_every_token_class(self):
        for name in _highlight.CLASSES:
            self.assertIn(f".{name} {{", _highlight.CSS)


class PerformanceGuardTest(unittest.TestCase):
    """Pathological inputs must stay linear (possessive quantifiers, no backtracking)."""

    def test_unterminated_string_is_fast(self):
        start = time.perf_counter()
        _highlight.highlight('"' + "a" * 200_000, "javascript")
        self.assertLess(time.perf_counter() - start, 2.0)

    def test_unterminated_block_comment_is_fast(self):
        start = time.perf_counter()
        _highlight.highlight("/*" + "a*" * 100_000, "c")
        self.assertLess(time.perf_counter() - start, 2.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
