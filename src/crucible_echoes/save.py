from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
import time

from .model import GameState


# Only the short commit phase is serialized. Preparing independent candidate
# files remains concurrent; independent processes still use unique files and
# bounded retries. This does not merge competing game states (last writer wins).
_SAVE_COMMIT_LOCK = RLock()


def save_game(state: GameState, path: str | Path) -> Path:
    target = Path(path)
    payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # A fixed .tmp name lets overlapping processes overwrite each other's
        # candidate state. Each writer owns a unique file on the same volume.
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                prefix=f".{target.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        with _SAVE_COMMIT_LOCK:
            for attempt in range(5):
                try:
                    temporary.replace(target)
                    break
                except PermissionError:
                    # Windows can transiently deny renames/scanner access.
                    # Persistent permission failures still propagate safely.
                    if attempt == 4:
                        raise
                    time.sleep(0.01)
    except BaseException:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Do not mask the original failure or remove another writer's file.
        raise
    return target


def load_game(path: str | Path) -> GameState:
    source = Path(path)
    return GameState.from_dict(json.loads(source.read_text(encoding="utf-8")))
