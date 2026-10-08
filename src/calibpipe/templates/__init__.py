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
    defaults: dict[str, Any] = {}
    if template_name == "casa_config.py.in":
        defaults = {
            "telemetry": "False",
            "log2term": "False",
            "casadata": "",
            "datapath": "None",
            "rundata": "None",
            "rundata_specified": "False",
        }
    elif template_name == "slurm_job.sh.in":
        p = kwargs.get("partition", kwargs.get("queue", "plwg"))
        defaults = {
            "partition": p,
            "queue": p,
        }
    str_kwargs = {**defaults, **{k: str(v) for k, v in kwargs.items()}}
    return Template(raw).safe_substitute(str_kwargs)
