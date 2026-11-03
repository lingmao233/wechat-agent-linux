"""Server-rendered console: Jinja2 templates, static assets and page views.

The HTTP routing still lives in ``wechat_agent.desktop`` (stdlib handler only).
Templates and static files live here so ``desktop.py`` never concatenates HTML
strings again.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

WEB_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

_environment: Environment | None = None


def environment() -> Environment:
    """Return the shared Jinja2 environment (autoescape on, files auto-reload)."""

    global _environment
    if _environment is None:
        _environment = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(("html", "xml")),
            trim_blocks=True,
            lstrip_blocks=True,
            auto_reload=True,
        )
    return _environment


def render(template: str, **context: object) -> str:
    return environment().get_template(template).render(**context)


# Imported last: ``views`` imports ``render`` from this module.
from .views import WebConsole  # noqa: E402  (public re-export)

__all__ = ["STATIC_DIR", "TEMPLATE_DIR", "WEB_DIR", "WebConsole", "environment", "render"]
