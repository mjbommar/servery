"""Markdown-subset renderer tests, with the safety model front and centre."""

import unittest

from servery import _markdown


class BlockTest(unittest.TestCase):
    def test_atx_headings_get_ids(self):
        out = _markdown.render("# Hello World\n\n## Sub\n")
        self.assertIn('<h1 id="hello-world">Hello World</h1>', out)
        self.assertIn('<h2 id="sub">Sub</h2>', out)

    def test_duplicate_heading_ids_are_unique(self):
        out = _markdown.render("# A\n\n# A\n")
        self.assertIn('id="a"', out)
        self.assertIn('id="a-1"', out)

    def test_closing_hashes_are_stripped(self):
        self.assertIn(">Title<", _markdown.render("## Title ##\n"))

    def test_setext_headings(self):
        out = _markdown.render("Title\n=====\n\nOther\n-----\n")
        self.assertIn("<h1", out)
        self.assertIn("<h2", out)

    def test_thematic_break(self):
        self.assertIn("<hr>", _markdown.render("a\n\n---\n\nb\n"))

    def test_paragraphs(self):
        out = _markdown.render("one\n\ntwo\n")
        self.assertEqual(out.count("<p>"), 2)

    def test_blockquote_nests(self):
        out = _markdown.render("> outer\n>\n> > inner\n")
        self.assertEqual(out.count("<blockquote>"), 2)

    def test_unordered_and_ordered_lists(self):
        out = _markdown.render("- a\n- b\n\n1. one\n2. two\n")
        self.assertIn("<ul><li>a</li><li>b</li></ul>", out)
        self.assertIn("<ol>", out)
        self.assertIn(">one<", out)

    def test_ordered_list_start_attribute(self):
        self.assertIn('<ol start="3">', _markdown.render("3. three\n4. four\n"))

    def test_nested_list(self):
        out = _markdown.render("- a\n  - b\n- c\n")
        self.assertEqual(out.count("<ul>"), 2)

    def test_loose_list_wraps_items_in_paragraphs(self):
        out = _markdown.render("- a\n\n- b\n")
        self.assertIn("<li><p>a</p></li>", out)

    def test_task_list(self):
        out = _markdown.render("- [ ] todo\n- [x] done\n")
        self.assertIn('<li class="task"><input type="checkbox" disabled> todo</li>', out)
        self.assertIn("disabled checked>", out)

    def test_fenced_code_is_highlighted_and_labelled(self):
        out = _markdown.render("```python\ndef f(): pass\n```\n")
        self.assertIn('data-lang="Python"', out)
        self.assertIn('<span class="k">def</span>', out)

    def test_fenced_code_without_info(self):
        out = _markdown.render("```\nplain <text>\n```\n")
        self.assertIn("&lt;text&gt;", out)
        self.assertNotIn("data-lang", out)

    def test_tilde_fence(self):
        self.assertIn("<pre", _markdown.render("~~~\ncode\n~~~\n"))

    def test_unclosed_fence_reaches_end_of_document(self):
        out = _markdown.render("```\nstill code\n")
        self.assertIn("still code", out)

    def test_indented_code_block(self):
        out = _markdown.render("text\n\n    indented\n")
        self.assertIn("<pre", out)
        self.assertIn("indented", out)

    def test_gfm_table_with_alignment(self):
        out = _markdown.render("| a | b |\n|:--|--:|\n| 1 | 2 |\n")
        self.assertIn("<table>", out)
        self.assertIn('style="text-align:right"', out)
        self.assertIn("<td>", out.replace(' style="text-align:left"', ""))

    def test_table_escaped_pipe_in_cell(self):
        out = _markdown.render("| a |\n| - |\n| x \\| y |\n")
        self.assertIn("x | y", out)

    def test_paragraph_interrupted_by_a_list(self):
        out = _markdown.render("text\n- item\n")
        self.assertIn("<p>text</p>", out)
        self.assertIn("<li>item</li>", out)


class InlineTest(unittest.TestCase):
    def test_emphasis_variants(self):
        out = _markdown.render("*a* _b_ **c** __d__ ~~e~~\n")
        self.assertIn("<em>a</em>", out)
        self.assertIn("<em>b</em>", out)
        self.assertIn("<strong>c</strong>", out)
        self.assertIn("<strong>d</strong>", out)
        self.assertIn("<del>e</del>", out)

    def test_intraword_underscore_is_not_emphasis(self):
        self.assertIn("snake_case_name", _markdown.render("snake_case_name\n"))

    def test_code_span_is_not_emphasised(self):
        out = _markdown.render("`a*b*c`\n")
        self.assertIn("<code>a*b*c</code>", out)
        self.assertNotIn("<em>", out)

    def test_code_span_with_doubled_backticks(self):
        self.assertIn("<code>a`b</code>", _markdown.render("``a`b``\n"))

    def test_backslash_escape_defeats_emphasis(self):
        out = _markdown.render("\\*not emphasis\\*\n")
        self.assertIn("*not emphasis*", out)
        self.assertNotIn("<em>", out)

    def test_inline_link_with_title(self):
        out = _markdown.render('[t](https://x.example/ "Title")\n')
        self.assertIn('href="https://x.example/"', out)
        self.assertIn('title="Title"', out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_reference_link_and_shortcut(self):
        out = _markdown.render("[ref]: https://r.example/\n\nSee [ref] and [text][ref].\n")
        self.assertEqual(out.count('href="https://r.example/"'), 2)

    def test_reference_defined_after_use(self):
        out = _markdown.render("See [ref].\n\n[ref]: https://later.example/\n")
        self.assertIn('href="https://later.example/"', out)

    def test_unresolved_reference_stays_literal(self):
        out = _markdown.render("See [nope].\n")
        self.assertIn("[nope]", out)
        self.assertNotIn("<a ", out)

    def test_autolink_and_bare_url(self):
        out = _markdown.render("<https://a.example/> and https://b.example/x\n")
        self.assertIn('href="https://a.example/"', out)
        self.assertIn('href="https://b.example/x"', out)

    def test_email_autolink(self):
        self.assertIn('href="mailto:me@example.com"', _markdown.render("<me@example.com>\n"))

    def test_bare_url_drops_trailing_punctuation(self):
        out = _markdown.render("see https://x.example/a.\n")
        self.assertIn('href="https://x.example/a"', out)

    def test_image(self):
        out = _markdown.render("![alt text](pic.png)\n")
        self.assertIn('<img src="pic.png" alt="alt text"', out)
        self.assertIn('loading="lazy"', out)

    def test_hard_break_two_spaces(self):
        self.assertIn("<br>", _markdown.render("a  \nb\n"))

    def test_hard_break_backslash(self):
        self.assertIn("<br>", _markdown.render("a\\\nb\n"))

    def test_nested_brackets_in_link_text(self):
        out = _markdown.render("[a [b] c](https://x.example/)\n")
        self.assertIn(">a [b] c</a>", out)


class FrontMatterTest(unittest.TestCase):
    def test_yaml_front_matter_is_skipped(self):
        out = _markdown.render("---\ntitle: T\n---\n\n# Body\n")
        self.assertNotIn("title: T", out)
        self.assertIn("Body", out)

    def test_toml_front_matter_is_skipped(self):
        out = _markdown.render('+++\ntitle = "T"\n+++\n\nBody\n')
        self.assertNotIn("title =", out)

    def test_leading_thematic_break_is_not_front_matter(self):
        out = _markdown.render("---\n\nreal content\n\n---\n")
        self.assertIn("real content", out)
        self.assertIn("<hr>", out)

    def test_split_front_matter_returns_parts(self):
        fence, front, body = _markdown.split_front_matter("---\na: 1\n---\nrest\n")
        self.assertEqual(fence, "---")
        self.assertEqual(front, "a: 1")
        self.assertEqual(body, "rest\n")


class SafetyTest(unittest.TestCase):
    def test_raw_html_is_escaped_not_passed_through(self):
        out = _markdown.render("<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n")
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)  # the attack tag never becomes markup
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", out)  # inert text

    def test_javascript_url_is_dropped_but_text_kept(self):
        out = _markdown.render("[click](javascript:alert)\n")
        self.assertNotIn("javascript:", out)
        self.assertIn("click", out)

    def test_data_url_image_is_dropped(self):
        out = _markdown.render("![x](data:text/html,<script>alert(1)</script>)\n")
        self.assertNotIn("data:", out)
        self.assertNotIn("<script", out)

    def test_scheme_check_ignores_embedded_control_characters(self):
        self.assertIsNone(_markdown.safe_url("java\tscript:alert(1)"))
        self.assertIsNone(_markdown.safe_url("  JAVASCRIPT:alert(1)"))
        self.assertIsNone(_markdown.safe_url("vbscript:x"))

    def test_relative_and_fragment_urls_are_allowed(self):
        self.assertEqual(_markdown.safe_url("./a/b.html"), "./a/b.html")
        self.assertEqual(_markdown.safe_url("#section"), "#section")
        self.assertEqual(_markdown.safe_url("https://ok.example/"), "https://ok.example/")

    def test_empty_url_is_rejected(self):
        self.assertIsNone(_markdown.safe_url("   "))

    def test_link_title_and_href_are_attribute_escaped(self):
        out = _markdown.render('[t](https://x.example/?a=1&b=2 "a<b&c")\n')
        self.assertIn('title="a&lt;b&amp;c"', out)
        self.assertIn('href="https://x.example/?a=1&amp;b=2"', out)

    def test_quote_in_an_image_alt_cannot_break_the_attribute(self):
        out = _markdown.render('![a" onerror=alert(1)](pic.png)\n')
        self.assertNotIn('" onerror=', out)
        self.assertIn("&quot;", out)

    def test_html_in_table_cells_and_headings_is_escaped(self):
        out = _markdown.render("# <b>x</b>\n\n| <i>h</i> |\n| - |\n| <u>c</u> |\n")
        self.assertNotIn("<b>", out)
        self.assertNotIn("<i>", out)
        self.assertNotIn("<u>", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
