"""Production entry point.

Parses ``--config`` and runs the ASGI app via uvicorn. systemd will invoke
this; ``uvicorn squatchtodo.api:app`` works for development.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from . import api as api_module
from .config import load_config


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="squatchtodo")
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    parser.add_argument("--bind", help="Override bind address (host)")
    parser.add_argument("--port", type=int, help="Override bind port")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.bind:
        config.server.bind_address = args.bind
    if args.port:
        config.server.port = args.port

    _configure_logging(config.log_level)

    app = api_module.create_app(config)

    uvicorn.run(
        app,
        host=config.server.bind_address,
        port=config.server.port,
        log_config=None,  # we configure the root logger ourselves
        access_log=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
