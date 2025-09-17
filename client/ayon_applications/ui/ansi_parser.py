"""ANSI to HTML converter for rendering colored terminal output in a web UI."""
import html
import re

class AnsiToHtmlConverter:
    """Convert ANSI escape sequences to HTML formatting."""

    # ANSI color codes to HTML colors
    ANSI_COLORS = {
        30: "#000000",  # Black
        31: "#AA0000",  # Red
        32: "#00AA00",  # Green
        33: "#AA5500",  # Yellow
        34: "#0000AA",  # Blue
        35: "#AA00AA",  # Magenta
        36: "#00AAAA",  # Cyan
        37: "#AAAAAA",  # White
        90: "#555555",  # Bright Black (Gray)
        91: "#FF5555",  # Bright Red
        92: "#55FF55",  # Bright Green
        93: "#FFFF55",  # Bright Yellow
        94: "#5555FF",  # Bright Blue
        95: "#FF55FF",  # Bright Magenta
        96: "#55FFFF",  # Bright Cyan
        97: "#FFFFFF",  # Bright White
    }

    # Background colors (add 10 to foreground codes)
    ANSI_BG_COLORS = {
        40: "#000000",  # Black
        41: "#AA0000",  # Red
        42: "#00AA00",  # Green
        43: "#AA5500",  # Yellow
        44: "#0000AA",  # Blue
        45: "#AA00AA",  # Magenta
        46: "#00AAAA",  # Cyan
        47: "#AAAAAA",  # White
        100: "#555555",  # Bright Black (Gray)
        101: "#FF5555",  # Bright Red
        102: "#55FF55",  # Bright Green
        103: "#FFFF55",  # Bright Yellow
        104: "#5555FF",  # Bright Blue
        105: "#FF55FF",  # Bright Magenta
        106: "#55FFFF",  # Bright Cyan
        107: "#FFFFFF",  # Bright White
    }

    def __init__(self):
        """Initialize the converter."""
        # Regex pattern to match ANSI escape sequences
        self.ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

        # More specific pattern for SGR (Select Graphic Rendition) sequences
        self.sgr_pattern = re.compile(r"\x1B\[([0-9;]*)m")

    def convert(self, text: str) -> str:
        """Convert text with ANSI codes to HTML.

        Args:
            text (str): Input text with ANSI escape sequences.

        Returns:
            str: HTML formatted text.
        """
        if not text:
            return ""

        # HTML escape the text first, but preserve ANSI sequences
        parts = []
        last_end = 0

        # Find all ANSI escape sequences
        for match in self.ansi_escape.finditer(text):
            # Add escaped text before this sequence
            if match.start() > last_end:
                parts.append(html.escape(text[last_end : match.start()]))

            # Add the ANSI sequence (unescaped for processing)
            parts.append(text[match.start() : match.end()])
            last_end = match.end()

        # Add remaining escaped text
        if last_end < len(text):
            parts.append(html.escape(text[last_end:]))

        # Now process the mixed content
        html_text = "".join(parts)

        # Track current formatting state
        current_fg = None
        current_bg = None
        bold = False
        italic = False
        underline = False

        result_parts = []
        open_spans = []

        def close_all_spans():
            """Close all open spans."""
            nonlocal open_spans
            for _ in open_spans:
                result_parts.append("</span>")
            open_spans.clear()

        def open_span():
            """Open a new span with current formatting."""
            styles = []
            if current_fg:
                styles.append(f"color: {current_fg}")
            if current_bg:
                styles.append(f"background-color: {current_bg}")
            if bold:
                styles.append("font-weight: bold")
            if italic:
                styles.append("font-style: italic")
            if underline:
                styles.append("text-decoration: underline")

            if styles:
                style_str = "; ".join(styles)
                result_parts.append(f'<span style="{style_str}">')
                open_spans.append("span")

        # Process SGR sequences
        last_pos = 0
        for match in self.sgr_pattern.finditer(html_text):
            # Add text before this sequence
            if match.start() > last_pos:
                if not open_spans and (
                    current_fg or current_bg or bold or italic or underline
                ):
                    open_span()
                result_parts.append(html_text[last_pos : match.start()])

            # Parse the SGR sequence
            params = match.group(1)
            if not params:
                params = "0"  # Default reset

            codes = [int(x) if x else 0 for x in params.split(";")]

            for code in codes:
                if code == 0:  # Reset all
                    close_all_spans()
                    current_fg = current_bg = None
                    bold = italic = underline = False
                elif code == 1:  # Bold
                    bold = True
                elif code == 3:  # Italic
                    italic = True
                elif code == 4:  # Underline
                    underline = True
                elif code == 22:  # Normal intensity (not bold)
                    bold = False
                elif code == 23:  # Not italic
                    italic = False
                elif code == 24:  # Not underlined
                    underline = False
                elif code in self.ANSI_COLORS:  # Foreground color
                    current_fg = self.ANSI_COLORS[code]
                elif code in self.ANSI_BG_COLORS:  # Background color
                    current_bg = self.ANSI_BG_COLORS[code]
                elif code == 39:  # Default foreground
                    current_fg = None
                elif code == 49:  # Default background
                    current_bg = None

            # Close previous span and open new one if needed
            if open_spans:
                close_all_spans()
            if current_fg or current_bg or bold or italic or underline:
                open_span()

            last_pos = match.end()

        # Add remaining text
        if last_pos < len(html_text):
            if not open_spans and (
                current_fg or current_bg or bold or italic or underline
            ):
                open_span()
            result_parts.append(html_text[last_pos:])

        # Close any remaining spans
        close_all_spans()

        # Convert newlines to <br> tags
        result = "".join(result_parts).replace("\n", "<br>")

        return f'<pre style="margin: 0; font-family: monospace;">{result}</pre>'

