"""Tests for the SNH client config example.

These guard the contract between SNH's config block and the SquatchTodo
server: if either side changes (port, header name, table key), the test
breaks before a deploy goes wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

try:  # 3.11+
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from squatchtodo.api import create_app
from squatchtodo.config import (
    Config,
    DatabaseConfig,
    IdentityConfig,
    McpConfig,
    ServerConfig,
    load_config,
)
from squatchtodo.mcp_server import IDENTITY_HEADER

REPO_ROOT = Path(__file__).resolve().parent.parent
SNH_EXAMPLE = REPO_ROOT / "deploy" / "snh-client.example.toml"
SERVER_EXAMPLE = REPO_ROOT / "deploy" / "config.example.toml"


def _load_snh_block() -> dict:
    return tomllib.loads(SNH_EXAMPLE.read_text())["tools"]["squatchtodo"]


# -- structural ------------------------------------------------------------


def test_snh_example_is_valid_toml_with_required_keys():
    block = _load_snh_block()
    assert isinstance(block["enabled"], bool)
    assert block["url"].startswith("http://") and block["url"].endswith("/mcp")
    assert isinstance(block["identity"], str) and block["identity"]


def test_snh_url_port_matches_server_default():
    """If config.example.toml ever changes the server port, the SNH snippet
    needs to follow — otherwise a fresh deploy would have SNH talking to a
    closed port. This is the cheapest way to catch that drift."""
    snh_url = _load_snh_block()["url"]
    m = re.search(r":(\d+)/mcp$", snh_url)
    assert m, snh_url
    snh_port = int(m.group(1))

    server_cfg = load_config(SERVER_EXAMPLE)
    assert snh_port == server_cfg.server.port


def test_snh_url_path_is_the_mounted_mcp_path():
    """``/mcp`` is what api.create_app mounts FastMCP on; the snippet must
    point at that path."""
    assert _load_snh_block()["url"].endswith("/mcp")


# -- live round-trip --------------------------------------------------------


def test_identity_from_snh_example_attributes_writes(tmp_path: Path):
    """Read the identity from the SNH example, send it as the standard
    header, and verify the server records it as ``created_by``."""
    block = _load_snh_block()
    identity = block["identity"]

    cfg = Config(
        server=ServerConfig(),
        database=DatabaseConfig(path=tmp_path / "snh.db"),
        identity=IdentityConfig(default="ellie"),
        mcp=McpConfig(enabled=False),
    )
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.post(
            "/api/projects",
            json={"name": "from-snh"},
            headers={IDENTITY_HEADER: identity},
        )
        assert r.status_code == 201, r.text
        assert r.json()["created_by"] == identity


def test_identity_header_constant_matches_server_config_default():
    """The header name documented in the SNH example must match the
    ``IDENTITY_HEADER`` constant the MCP middleware actually checks."""
    server_cfg = load_config(SERVER_EXAMPLE)
    assert server_cfg.identity.header_name == IDENTITY_HEADER
