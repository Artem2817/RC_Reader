from __future__ import annotations


def run() -> None:
    """Launch the Qt desktop application without importing Qt in core-only use."""
    try:
        from .ui.main_window import run_app
    except ImportError as exc:
        if exc.name and exc.name.startswith("PySide6"):
            raise SystemExit(
                "PySide6 is required for the desktop interface. "
                "Install dependencies with: pip install -r requirements.txt"
            ) from exc
        raise
    raise SystemExit(run_app())
