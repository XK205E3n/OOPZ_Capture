"""OOPZ digest V7 renderer: offline, deterministic, measure-first."""
from .render import RenderResult, render_digest_card, verify_text_consistency  # noqa: F401
from .textlayout import MissingGlyph, MissingResource, RenderError  # noqa: F401

__version__ = "7.0.0"
