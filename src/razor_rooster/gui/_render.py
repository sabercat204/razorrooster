"""Tiny helper that wraps Jinja TemplateResponse with a typed return.

Starlette's ``Jinja2Templates.TemplateResponse`` is annotated to
return ``Any``, which makes ``mypy --strict`` complain about every
route that returns it directly. This helper wraps the call and
returns a typed ``Response``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import Response


def render_template(
    request: Request,
    template_name: str,
    context: dict[str, Any],
) -> Response:
    """Render ``template_name`` against ``context`` and return a Response."""
    templates = request.app.state.templates
    response: Response = templates.TemplateResponse(request, template_name, context)
    return response


__all__ = ["render_template"]
