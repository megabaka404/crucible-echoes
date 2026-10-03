"""Launch the pywebview desktop client.

Run from the repository root with ``python -m desktop.app``.  pywebview is an
optional desktop dependency so the existing CLI and tests remain dependency
free.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

def resource_root() -> Path:
    """Return source or PyInstaller bundle root without using cwd."""

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parents[1]


ROOT = resource_root()
SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT / "src"))

try:
    from .bridge import DesktopBridge
except ImportError:  # pragma: no cover - useful when PyInstaller executes the file directly
    from desktop.bridge import DesktopBridge


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crucible Echoes Windows desktop client")
    parser.add_argument("--save", default=None, help="JSON save path (default: local user data directory)")
    parser.add_argument("--debug", action="store_true", help="enable pywebview debug mode")
    parser.add_argument("--smoke-test", action="store_true", help="open a real-window bridge smoke test and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        import webview  # type: ignore[import-not-found]
    except ImportError:
        print("缺少 pywebview。请先运行：python -m pip install -e .[desktop]", file=sys.stderr)
        return 2

    if args.smoke_test:
        try:
            from .smoke import main as smoke_main
        except ImportError:  # pragma: no cover - PyInstaller direct-script fallback
            from desktop.smoke import main as smoke_main
        return smoke_main()

    bridge = DesktopBridge(args.save)
    log_dir = bridge._save_path.parent
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_dir / "crucible-echoes.log"),
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    try:
        window = webview.create_window(
            "坩埚余响 · Crucible Echoes",
            str(ROOT / "frontend" / "index.html"),
            js_api=bridge,
            width=1280,
            height=760,
            min_size=(980, 640),
            resizable=True,
        )
        bridge._window = window
        webview.start(debug=args.debug)
    except Exception:
        logging.exception("Desktop client crashed")
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
