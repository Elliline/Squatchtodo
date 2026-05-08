"""Configuration loading.

Config is read from a TOML file (default ``/etc/squatchtodo/config.toml``) with
every field optional — sensible defaults let the server boot on a fresh box
with an empty config, and ``SQUATCHTODO_*`` env vars override individual
settings for development.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # 3.11+
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_CONFIG_PATH = Path("/etc/squatchtodo/config.toml")


@dataclass
class ServerConfig:
    bind_address: str = "127.0.0.1"
    port: int = 3100


@dataclass
class DatabaseConfig:
    path: Path = Path("/var/lib/squatchtodo/squatchtodo.db")


@dataclass
class IdentityConfig:
    default: str = "ellie"
    header_name: str = "X-Squatchtodo-Identity"


@dataclass
class McpConfig:
    enabled: bool = True


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    log_level: str = "INFO"


def _from_dict(data: dict) -> Config:
    server = ServerConfig(**data.get("server", {}))
    db_section = dict(data.get("database", {}))
    if "path" in db_section:
        db_section["path"] = Path(db_section["path"]).expanduser()
    database = DatabaseConfig(**db_section)
    identity = IdentityConfig(**data.get("identity", {}))
    mcp = McpConfig(**data.get("mcp", {}))
    return Config(
        server=server,
        database=database,
        identity=identity,
        mcp=mcp,
        log_level=data.get("log_level", "INFO"),
    )


def _apply_env_overrides(cfg: Config) -> Config:
    """Apply ``SQUATCHTODO_*`` env vars on top of the loaded config.

    Useful for dev (point at a temp DB without editing /etc) and for systemd
    drop-ins that only want to flip one setting.
    """
    if (v := os.environ.get("SQUATCHTODO_BIND_ADDRESS")) is not None:
        cfg.server.bind_address = v
    if (v := os.environ.get("SQUATCHTODO_PORT")) is not None:
        cfg.server.port = int(v)
    if (v := os.environ.get("SQUATCHTODO_DB_PATH")) is not None:
        cfg.database.path = Path(v).expanduser()
    if (v := os.environ.get("SQUATCHTODO_IDENTITY_DEFAULT")) is not None:
        cfg.identity.default = v
    if (v := os.environ.get("SQUATCHTODO_LOG_LEVEL")) is not None:
        cfg.log_level = v
    if (v := os.environ.get("SQUATCHTODO_MCP_ENABLED")) is not None:
        cfg.mcp.enabled = v.lower() in ("1", "true", "yes", "on")
    return cfg


def load_config(path: Path | str | None = None) -> Config:
    """Load config from ``path`` (or the default location) and apply env overrides.

    Missing files are tolerated — a fully default config is returned. This
    matches the deploy expectation that ``/etc/squatchtodo/config.toml`` may
    not exist on a fresh box but the service should still come up listening
    on localhost.
    """
    config_path = Path(path) if path else Path(
        os.environ.get("SQUATCHTODO_CONFIG", DEFAULT_CONFIG_PATH)
    )
    if config_path.exists():
        with config_path.open("rb") as fp:
            data = tomllib.load(fp)
        cfg = _from_dict(data)
    else:
        cfg = Config()
    return _apply_env_overrides(cfg)
