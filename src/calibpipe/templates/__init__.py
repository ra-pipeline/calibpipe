"""Template loading and rendering utilities for calibpipe."""

from __future__ import annotations

from importlib.resources import files
from string import Template
from typing import Any


def render_template(template_name: str, **kwargs: Any) -> str:
    """Read and render a template from the calibpipe.templates package.

    Args:
        template_name: Name of the template file in calibpipe/templates.
        **kwargs: Variables to substitute into the template.

    Returns:
        Rendered template string with variables substituted.
    """
    raw = files("calibpipe.templates").joinpath(template_name).read_text(encoding="utf-8")
    # Convert all keyword arguments to string representation for safe substitution
    str_kwargs = {k: str(v) for k, v in kwargs.items()}
    return Template(raw).safe_substitute(str_kwargs)
