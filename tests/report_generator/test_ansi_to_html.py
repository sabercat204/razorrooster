"""Tests for the ANSI SGR → HTML translator (v0.46.0 follow-on Step 4).

Covers:
- strip_ansi removes every CSI sequence (SGR, cursor, screen).
- ansi_to_html maps 8 standard + 8 bright foreground colors to
  semantic CSS class spans.
- ansi_to_html maps bold / dim / italic / underline to spans.
- Reset codes (0, 39) close open spans correctly.
- Plain text without ANSI passes through unchanged (HTML-escaped).
- HTML-special characters in the text get escaped.
- Malformed sequences are silently dropped.
- Unknown SGR codes (e.g. background colors, 256-color) are
  silently dropped.
- Output is well-nested (every <span> has a matching </span>).
"""

from __future__ import annotations

from razor_rooster.report_generator.engines.ansi_to_html import (
    ANSI_INLINE_CSS,
    ansi_to_html,
    strip_ansi,
)

# -- strip_ansi tests ---------------------------------------------------


def test_strip_ansi_removes_sgr_sequences() -> None:
    text = "\x1b[31mred text\x1b[0m and plain"
    assert strip_ansi(text) == "red text and plain"


def test_strip_ansi_removes_cursor_sequences() -> None:
    text = "before\x1b[2Aafter"  # cursor up
    assert strip_ansi(text) == "beforeafter"


def test_strip_ansi_passthrough_plain_text() -> None:
    text = "plain text with no escapes"
    assert strip_ansi(text) == text


def test_strip_ansi_handles_empty_string() -> None:
    assert strip_ansi("") == ""


def test_strip_ansi_handles_multiple_concatenated_sequences() -> None:
    text = "\x1b[1m\x1b[31mwarn\x1b[0m"
    assert strip_ansi(text) == "warn"


# -- ansi_to_html — foreground colors -----------------------------------


def test_ansi_to_html_red_foreground() -> None:
    out = ansi_to_html("\x1b[31mred\x1b[0m")
    assert '<span class="ansi-fg-red">red</span>' in out


def test_ansi_to_html_all_eight_standard_colors() -> None:
    colors = [
        (30, "ansi-fg-black"),
        (31, "ansi-fg-red"),
        (32, "ansi-fg-green"),
        (33, "ansi-fg-yellow"),
        (34, "ansi-fg-blue"),
        (35, "ansi-fg-magenta"),
        (36, "ansi-fg-cyan"),
        (37, "ansi-fg-white"),
    ]
    for code, css_class in colors:
        text = f"\x1b[{code}mhello\x1b[0m"
        out = ansi_to_html(text)
        assert f'class="{css_class}"' in out
        assert "hello" in out


def test_ansi_to_html_bright_colors() -> None:
    text = "\x1b[91mbright red\x1b[0m"
    out = ansi_to_html(text)
    assert "ansi-fg-bright-red" in out


def test_ansi_to_html_reset_fg_only() -> None:
    """SGR 39 closes only the foreground span, leaving attributes alone."""
    text = "\x1b[1m\x1b[31mboth\x1b[39mbold-only\x1b[0m"
    out = ansi_to_html(text)
    assert "ansi-fg-red" in out
    assert "ansi-bold" in out
    assert "bold-only" in out


# -- ansi_to_html — typographic attributes ------------------------------


def test_ansi_to_html_bold() -> None:
    text = "\x1b[1mBOLD\x1b[0m"
    out = ansi_to_html(text)
    assert '<span class="ansi-bold">BOLD</span>' in out


def test_ansi_to_html_dim() -> None:
    text = "\x1b[2mdim\x1b[0m"
    out = ansi_to_html(text)
    assert "ansi-dim" in out


def test_ansi_to_html_italic() -> None:
    text = "\x1b[3mitalic\x1b[0m"
    out = ansi_to_html(text)
    assert "ansi-italic" in out


def test_ansi_to_html_underline() -> None:
    text = "\x1b[4munderline\x1b[0m"
    out = ansi_to_html(text)
    assert "ansi-underline" in out


def test_ansi_to_html_combined_attrs_and_color() -> None:
    text = "\x1b[1;31mbold-red\x1b[0m"
    out = ansi_to_html(text)
    assert "ansi-bold" in out
    assert "ansi-fg-red" in out
    assert "bold-red" in out


# -- ansi_to_html — escaping & passthrough -----------------------------


def test_ansi_to_html_escapes_html_special() -> None:
    text = '<script>alert(1)</script>\n& " '
    out = ansi_to_html(text)
    assert "&lt;script&gt;" in out
    assert "&amp;" in out
    assert "&quot;" in out
    assert "<script>" not in out


def test_ansi_to_html_plain_text_passthrough() -> None:
    text = "no escapes here"
    assert ansi_to_html(text) == "no escapes here"


def test_ansi_to_html_empty_string() -> None:
    assert ansi_to_html("") == ""


def test_ansi_to_html_preserves_newlines_and_whitespace() -> None:
    text = "\x1b[31mfirst\x1b[0m\n  indented second"
    out = ansi_to_html(text)
    assert "\n  indented second" in out


# -- ansi_to_html — robustness -----------------------------------------


def test_ansi_to_html_unknown_sgr_dropped() -> None:
    """Background colors (40-47) aren't in the palette; their codes get dropped."""
    text = "\x1b[41mwith bg\x1b[0m"
    out = ansi_to_html(text)
    # Text is preserved.
    assert "with bg" in out
    # No span for unknown SGR.
    assert "ansi-fg" not in out
    # No raw escape.
    assert "\x1b" not in out


def test_ansi_to_html_256_color_dropped() -> None:
    text = "\x1b[38;5;196mhi\x1b[0m"  # SGR 256-color: 38;5;<n>
    out = ansi_to_html(text)
    assert "hi" in out
    # 38 by itself isn't in the palette and the 256-color subcommand
    # isn't supported.
    assert "ansi-fg" not in out


def test_ansi_to_html_malformed_sequence_dropped() -> None:
    """An unterminated escape leaves only the visible bytes."""
    text = "before\x1b[31mafter"  # no reset
    out = ansi_to_html(text)
    assert "before" in out
    assert "after" in out


def test_ansi_to_html_well_nested_output() -> None:
    """Every <span> has a matching </span>."""
    text = "\x1b[1m\x1b[31mred-bold\x1b[0m plain"
    out = ansi_to_html(text)
    open_count = out.count("<span")
    close_count = out.count("</span>")
    assert open_count == close_count


def test_ansi_to_html_reset_closes_all_open_spans() -> None:
    text = "\x1b[1m\x1b[31mboth\x1b[0mafter"
    out = ansi_to_html(text)
    open_count = out.count("<span")
    close_count = out.count("</span>")
    assert open_count == close_count
    assert "after" in out
    # The "after" segment shouldn't carry any span around it.
    assert out.endswith("after")


def test_ansi_inline_css_includes_palette_classes() -> None:
    """The inline-CSS block declares the classes ansi_to_html emits."""
    assert ".ansi-bold" in ANSI_INLINE_CSS
    assert ".ansi-fg-red" in ANSI_INLINE_CSS
    assert ".ansi-fg-bright-red" in ANSI_INLINE_CSS
    assert ".ansi-italic" in ANSI_INLINE_CSS
    assert ".ansi-underline" in ANSI_INLINE_CSS
    assert ".ansi-dim" in ANSI_INLINE_CSS
