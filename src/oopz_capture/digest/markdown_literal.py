"""Literal Markdown for exactly the renderer-visible text."""
import html
import re

def visible_text_markdown(text: list[str]) -> bytes:
    """Literal CommonMark paragraphs: display the same characters, not markup.

    Entities/escapes prevent roster names from becoming HTML, links, headings,
    images, tables, emphasis or code when the companion is opened as Markdown.
    """
    def escape(block: str) -> str:
        escaped = html.escape(block, quote=False)
        return re.sub(r"([\\`*_{}\[\]()#+.!|~=:/@-])", r"\\\1", escaped)
    return ("\n\n".join(escape(block) for block in text) + "\n").encode("utf-8")
