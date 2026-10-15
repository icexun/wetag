"""Centralised configuration — Spring-style property management for Python.

Precedence (highest → lowest):
  process env → .env → config/workspaces/<WORKSPACE_ID>.yaml → config/wetag.env → Value defaults

Workspace YAML is flattened into env-style keys: ``tapd: {project_id: 1}`` → ``TAPD_PROJECT_ID``.

Usage:
    import config
    config.init()
    value = config.get("MODEL_NAME")

    from config.properties import ModelProperties
    ModelProperties.instance().name
"""
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_initialized = False


def _flatten(node: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(node, dict):
        out: dict[str, str] = {}
        for key, value in node.items():
            name = f"{prefix}_{key}" if prefix else str(key)
            out.update(_flatten(value, name))
        return out
    if node is None:
        return {}
    if isinstance(node, bool):
        return {prefix.upper(): "true" if node else "false"}
    if isinstance(node, (list, tuple)):
        import json

        return {prefix.upper(): json.dumps(node, ensure_ascii=False)}
    return {prefix.upper(): str(node)}


def load_workspace_file(workspace_id: str, directory: Path | None = None) -> dict[str, str]:
    """Read ``config/workspaces/<id>.yaml`` into flat env-style keys; missing file → {}."""
    path = (directory or _CONFIG_DIR / "workspaces") / f"{workspace_id}.yaml"
    if not path.is_file():
        return {}
    import yaml

    text = _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), path.read_text(encoding="utf-8"))
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a mapping")
    return _flatten(data)


def init():
    """Merge .env, workspace YAML and config/wetag.env into ``os.environ``. Call once at startup.

    Each layer only fills keys still unset, so earlier layers win.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    try:
        from dotenv import dotenv_values, load_dotenv
    except ImportError:
        dotenv_values = load_dotenv = None

    if load_dotenv is not None:
        load_dotenv()  # .env：机密（gitignored）
    defaults = dict(dotenv_values(_CONFIG_DIR / "wetag.env")) if dotenv_values else {}

    workspace_id = os.environ.get("WORKSPACE_ID") or defaults.get("WORKSPACE_ID")
    if workspace_id:
        values = load_workspace_file(workspace_id)
        if values:
            logger.info("Workspace config loaded: config/workspaces/%s.yaml", workspace_id)
        _merge(values)

    _merge(defaults)


def _merge(values: dict) -> None:
    for key, value in values.items():
        if value not in (None, "") and not os.environ.get(key):
            os.environ[key] = str(value)


_SECRET_KEYS = frozenset({"API_KEY", "api_key", "WECOM_BOT_SECRET", "PG_DSN"})


def get(key: str, default=None):
    """Resolve config value: env → default."""
    env_val = os.environ.get(key)
    if env_val:
        return env_val
    return default


def get_int(key: str, default: int = 0) -> int:
    return _cast(get(key, default), default)


def get_float(key: str, default: float = 0.0) -> float:
    return _cast(get(key, default), default)


def get_bool(key: str, default: bool = False) -> bool:
    return _cast(get(key, default), default)


def _cast(value, default):
    """Cast a string config value to match the type of *default*."""
    if value is None or value is default:
        return default
    try:
        # bool must be checked before int (bool is subclass of int in Python)
        if isinstance(default, bool):
            return str(value).lower() in ("true", "1", "yes")
        if isinstance(default, int):
            return int(value)
        if isinstance(default, float):
            return float(value)
    except (ValueError, TypeError):
        return default
    return value


# ── Spring-like Configuration Annotations ─────────────────────────────────────


class Value:
    """Spring ``@Value`` equivalent — resolves config on each property access.

    Type is inferred from the *default* value and auto-cast from the string value.
    As a non-data descriptor, instance-level assignment overrides the resolved value.
    """

    def __init__(self, key: str, default=None):
        self.key = key
        self.default = default
        self._attr_name = None

    def __set_name__(self, owner, name):
        self._attr_name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return _cast(get(self.key, self.default), self.default)

    def __repr__(self):
        return f"Value('{self.key}', default={self.default!r})"


class ConfigurationProperties:
    """Spring ``@ConfigurationProperties`` equivalent — typed config grouping."""

    _instances: dict = {}

    @classmethod
    def instance(cls):
        """Return singleton instance."""
        if cls not in ConfigurationProperties._instances:
            ConfigurationProperties._instances[cls] = cls()
        return ConfigurationProperties._instances[cls]

    def __repr__(self):
        cls = type(self)
        fields = []
        for name in sorted(vars(cls)):
            descriptor = getattr(cls, name, None)
            if isinstance(descriptor, Value):
                resolved = getattr(self, name)
                if descriptor.key in _SECRET_KEYS:
                    resolved = "***"
                fields.append(f"{name}={resolved!r}")
        return f"{cls.__name__}({', '.join(fields)})"
