"""
H1 — Plugin discovered + UI panel rendered.

Design-doc invariant (§8):
  The autoresearch plugin is visible in A0's plugin registry AND the panel HTML
  contains valid Alpine.js bindings that reference 'autoresearch'.

Local assertions:
  1. plugin.yaml declares name: autoresearch
  2. panel.html contains x-data / x-init (Alpine binding present)
  3. panel.html references 'autoresearch' (case-insensitive)

The VPS portion of this gate (live HTTP panel render) is folded into H9.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_YAML = _PLUGIN_ROOT / "plugin.yaml"
_PANEL_HTML = _PLUGIN_ROOT / "webui" / "panel.html"


@pytest.mark.acceptance
def test_h1_plugin_yaml_declares_autoresearch():
    """plugin.yaml must exist and declare name: autoresearch."""
    assert _PLUGIN_YAML.exists(), f"plugin.yaml not found at {_PLUGIN_YAML}"
    data = yaml.safe_load(_PLUGIN_YAML.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "plugin.yaml must be a YAML mapping"
    assert data.get("name") == "autoresearch", (
        f"plugin.yaml name must be 'autoresearch', got {data.get('name')!r}"
    )


@pytest.mark.acceptance
def test_h1_plugin_yaml_has_required_fields():
    """plugin.yaml must contain title, description, and version fields."""
    data = yaml.safe_load(_PLUGIN_YAML.read_text(encoding="utf-8"))
    for field in ("title", "description", "version"):
        assert field in data, f"plugin.yaml missing required field: {field!r}"


@pytest.mark.acceptance
def test_h1_panel_html_exists():
    """panel.html must exist in webui/."""
    assert _PANEL_HTML.exists(), f"panel.html not found at {_PANEL_HTML}"


@pytest.mark.acceptance
def test_h1_panel_html_has_alpine_binding():
    """panel.html must contain at least one Alpine.js binding (x-data or x-init)."""
    html = _PANEL_HTML.read_text(encoding="utf-8")
    has_binding = "x-data" in html or "x-init" in html
    assert has_binding, (
        "panel.html does not contain any Alpine.js bindings (x-data or x-init)"
    )


@pytest.mark.acceptance
def test_h1_panel_html_references_autoresearch():
    """panel.html must reference 'autoresearch' (plugin name) at least once."""
    html = _PANEL_HTML.read_text(encoding="utf-8")
    assert "autoresearch" in html.lower(), (
        "panel.html does not reference 'autoresearch' — Alpine store may be disconnected"
    )


@pytest.mark.acceptance
def test_h1_plugin_module_imports_cleanly():
    """The autoresearch plugin package must be importable without errors."""
    import importlib
    spec = importlib.util.find_spec("usr.plugins.autoresearch")
    assert spec is not None, "usr.plugins.autoresearch package not found on sys.path"
    mod = importlib.import_module("usr.plugins.autoresearch")
    assert mod is not None
