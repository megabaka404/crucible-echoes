"""Small source/catalog snapshots for reproducible local balance experiments."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile

from .catalog import Catalog


def capture_snapshot(destination: Path, catalog: Catalog) -> dict:
    root = Path(__file__).resolve().parent
    # Explicit allowlist: no saves, credentials, environment or Git metadata.
    sources = {"crucible_echoes/" + path.name: path.read_bytes()
               for path in sorted(root.glob("*.py"))}
    sources["catalog.json"] = json.dumps(asdict(catalog), ensure_ascii=False, sort_keys=True).encode("utf-8")
    # Preserve definition insertion order: weighted draws depend on it.
    # Package the passed catalog, never possibly changed disk tables.
    for collection in ("ingredients", "items", "essences"):
        sources[f"crucible_echoes/data/{collection}.json"] = json.dumps(
            list(getattr(catalog, collection).values()), ensure_ascii=False
        ).encode("utf-8")
    sources["crucible_echoes/data/progression.json"] = json.dumps(catalog.progression, ensure_ascii=False).encode("utf-8")
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
    manifest = {"python": sys.version, "files": hashes,
                "scope": "Runnable simulation package and passed catalog resources; candidate patches are in the experiment report."}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for name, data in sources.items():
            archive.writestr(name, data)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"path": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "files": hashes}
