"""The hot-reload configuration plane (FR-CFG-1..5).

Two consumption styles, matching the PRD's two reload paths (Figure 6):

1. `read_prompt(path)` - Markdown prompt files are read fresh on every call.
   The agent's instruction provider calls this per LLM invocation, so prompt
   edits apply on the very next message with zero machinery.

2. `HotConfig` - structured YAML with validate-then-swap semantics:
     cfg = HotConfig(path, validator=SomeModel.model_validate)
     cfg.value            # current validated value (last known good)
     await cfg.watch(...) # watchfiles loop; on change: parse + validate,
                          # atomic swap on success, keep last known good and
                          # log the error on failure (FR-CFG-3)

Env interpolation: string values support ${VAR} and ${VAR:default}
(FR-CFG-5), resolved at load time so a reload also picks up... the same
env (env is process-immutable; the syntax exists so committed config never
needs machine-specific values).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog
import yaml

log = structlog.get_logger("cloudops.config")

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _interpolate(value: Any) -> Any:
    """Resolve ${VAR} / ${VAR:default} inside every string, recursively."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def load_yaml(path: Path) -> Any:
    """Parse a YAML file with env interpolation applied."""
    with path.open("r", encoding="utf-8") as fh:
        return _interpolate(yaml.safe_load(fh))


def read_prompt(path: Path) -> str:
    """Read a prompt/skill/template file fresh (the per-invocation path)."""
    return path.read_text(encoding="utf-8")


def config_version(paths: list[Path]) -> str:
    """Short content hash across config files, shown in the console's left
    rail so operators can see a reload landed (user flow F9)."""
    digest = hashlib.sha1()
    for p in sorted(paths):
        try:
            digest.update(p.read_bytes())
        except OSError:
            digest.update(b"missing:" + str(p).encode())
    return digest.hexdigest()[:7]


class HotConfig[T]:
    """A validated YAML file with last-known-good hot reload."""

    def __init__(
        self,
        path: Path,
        validator: Callable[[Any], T],
        on_reload: Callable[[T], Awaitable[None] | None] | None = None,
    ) -> None:
        self.path = path
        self._validator = validator
        self._on_reload = on_reload
        self._value: T = self._load()  # first load MUST succeed: fail fast at boot
        log.info("config.loaded", file=str(path))

    def _load(self) -> T:
        return self._validator(load_yaml(self.path))

    @property
    def value(self) -> T:
        return self._value

    async def reload(self) -> bool:
        """Parse + validate + swap. Returns True when a new version landed.

        On any error the previous value stays active (FR-CFG-3) and the
        validation error is logged at warning so operators see it.
        """
        try:
            new_value = self._load()
        except Exception as exc:  # noqa: BLE001 - any config error must not crash the service
            log.warning("config.reload_rejected", file=str(self.path), error=str(exc))
            return False
        self._value = new_value
        log.info("config.reload", file=str(self.path))
        if self._on_reload is not None:
            result = self._on_reload(new_value)
            if asyncio.iscoroutine(result):
                await result
        return True


async def watch_configs(*configs: HotConfig[Any]) -> None:
    """One watchfiles loop reloading whichever HotConfig changed.

    Run as a background task from each service's lifespan. Debouncing is
    handled by watchfiles itself; a change event triggers exactly one
    reload attempt per touched file.
    """
    from watchfiles import awatch  # local import: optional at unit-test time

    by_path = {str(c.path.resolve()): c for c in configs}
    async for changes in awatch(*by_path.keys()):
        for _change, changed_path in changes:
            cfg = by_path.get(str(Path(changed_path).resolve()))
            if cfg is not None:
                await cfg.reload()
