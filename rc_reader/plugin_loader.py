from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
from typing import Any, Callable

from .profiles import ProtocolSpec


@dataclass(frozen=True, slots=True)
class ParserPlugin:
    spec: ProtocolSpec
    factory: Callable[[], Any]
    path: Path


_CACHE: tuple[ParserPlugin, ...] | None = None


def plugin_directory() -> Path:
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
    return base / "plugins"


def load_plugins(force: bool = False) -> tuple[ParserPlugin, ...]:
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    directory = plugin_directory()
    found: list[ParserPlugin] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                module_name = f"rc_reader_external_{path.stem}"
                module_spec = importlib.util.spec_from_file_location(module_name, path)
                if module_spec is None or module_spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(module_spec)
                module_spec.loader.exec_module(module)
                metadata = module.PROTOCOL_SPEC
                spec = ProtocolSpec(
                    str(metadata["name"]),
                    int(metadata["baudrate"]),
                    str(metadata.get("uart_format", "8N1")).upper(),
                    str(metadata.get("wiring_note", "External parser plugin")),
                )
                factory = module.create_parser
                found.append(ParserPlugin(spec, factory, path))
            except Exception:
                continue
    _CACHE = tuple(found)
    return _CACHE


def find_plugin(protocol: str) -> ParserPlugin | None:
    normalized = protocol.strip().upper()
    return next(
        (plugin for plugin in load_plugins() if plugin.spec.name.upper() == normalized),
        None,
    )
