"""Renderer stage: one self-contained HTML page (ARCHITECTURE AD-09)."""

from .render import (
    CSP_TEMPLATE,
    RenderError,
    build_payload,
    csp_for,
    decode_payload,
    encode_payload,
    load_vendor,
    run_render,
    script_hashes,
)

__all__ = [
    "CSP_TEMPLATE",
    "RenderError",
    "build_payload",
    "csp_for",
    "decode_payload",
    "encode_payload",
    "load_vendor",
    "run_render",
    "script_hashes",
]
