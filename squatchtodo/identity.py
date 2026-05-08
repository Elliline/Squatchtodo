"""Actor identity resolution for the REST API.

The ``created_by`` / ``author`` field on each row records *who* did the thing.
Per the v1 spec:

* The web UI doesn't authenticate — assume the configured human identity
  (``ellie`` by default).
* MCP clients (SNH instances, Claude API sessions, Sparky's coder) send
  ``X-Squatchtodo-Identity`` with their registered identity.
* No validation in v1 — local network only. v2 swaps this dependency for one
  that checks a token and rejects unknown identities.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Header, Request

from .config import Config


async def resolve_identity(
    request: Request,
    x_squatchtodo_identity: Optional[str] = Header(
        default=None, alias="X-Squatchtodo-Identity"
    ),
) -> str:
    """Return the actor identity to attribute writes to."""
    config: Config = request.app.state.config
    if x_squatchtodo_identity and x_squatchtodo_identity.strip():
        return x_squatchtodo_identity.strip()
    return config.identity.default
