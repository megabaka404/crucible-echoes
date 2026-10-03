"""Run a short real-window smoke test for pywebview and the JS bridge."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from uuid import uuid4
from pathlib import Path

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import webview  # type: ignore[import-not-found]

from .bridge import DesktopBridge


class SmokeBridge(DesktopBridge):
    def __init__(self, save_path: str | Path) -> None:
        super().__init__(save_path)
        self._result_event = threading.Event()
        self._result: dict | None = None

    def smoke_result(self, result: str) -> dict[str, bool]:
        return {"ok": self._finish(json.loads(result))}

    def _finish(self, result: dict) -> bool:
        with self._lock:
            # First completion wins; a callback arriving after timeout must
            # not replace the failed result with a late success.
            if self._result_event.is_set():
                return False
            self._result = result
            self._result_event.set()
            if self._window is not None:
                closer = threading.Timer(0.15, self._window.destroy)
                closer.daemon = True
                closer.start()
            return True

    def _timeout(self) -> None:
        self._finish({"ok": False, "error": "bridge callback timeout after 30 seconds"})


def main() -> int:
    run_id = uuid4().hex
    result_path = Path.cwd() / "desktop-smoke-result.json"
    # Mark the new run immediately so a failed launch cannot leave a previous
    # successful JSON file looking like verification of this build.
    result_path.write_text(json.dumps({"ok": False, "pending": True, "run_id": run_id}), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="crucible-echoes-smoke-") as directory:
        save_path = Path(directory) / "smoke.json"
        bridge = SmokeBridge(save_path)
        window = webview.create_window(
            "Crucible Echoes · bridge smoke",
            str(ROOT / "frontend" / "index.html") + "?smoke=1",
            js_api=bridge,
            width=960,
            height=640,
            min_size=(800, 560),
        )
        bridge._window = window
        watchdog = threading.Timer(30, bridge._timeout)
        watchdog.daemon = True
        watchdog.start()
        try:
            webview.start(debug=False)
        finally:
            watchdog.cancel()
        if not bridge._result_event.is_set():
            bridge._finish({"ok": False, "error": "window closed before bridge callback"})
        result = bridge._result or {"ok": False, "error": "empty result"}
        result.update(run_id=run_id, renderer=webview.renderer)
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
