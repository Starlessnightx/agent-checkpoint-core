"""Append-only object pack for exchange archives.

Each checkpoint appends only objects that are not already in the pack and
stores a small manifest that points at byte ranges.  Save cost is proportional
to new bytes (typically a few hundred KB per step); restore seeks and writes
exactly the recorded ranges.  Content is byte-identical to the source tree.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

HEADER_BYTES = 4


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PackStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.pack_path = self.root / "objects.pack"
        self.index_path = self.root / "objects.index.json"
        self.index: dict[str, list[int]] = {}
        if self.index_path.exists():
            self.index = json.loads(self.index_path.read_text())

    def _append(self, digest: str, data: bytes) -> list[int]:
        with self.pack_path.open("ab") as handle:
            offset = handle.tell()
            header = json.dumps({"sha256": digest, "size": len(data)}).encode()
            handle.write(len(header).to_bytes(HEADER_BYTES, "big"))
            handle.write(header)
            handle.write(data)
        entry = [offset, len(data)]
        self.index[digest] = entry
        return entry

    def flush_index(self) -> None:
        self.index_path.write_text(json.dumps(self.index, sort_keys=True))

    def add_archive(self, source: Path) -> dict:
        """Append new objects from ``source`` and return its manifest entries."""
        source = Path(source)
        files: dict[str, list] = {}
        total = 0
        for path in sorted(source.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = str(path.relative_to(source))
            data = path.read_bytes()
            digest = _sha256_bytes(data)
            entry = self.index.get(digest)
            if entry is None:
                entry = self._append(digest, data)
            files[relative] = [digest, entry[1]]
            total += entry[1]
        return {"files": files, "size": total}

    def read_object(self, digest: str) -> bytes:
        offset, size = self.index[digest]
        with self.pack_path.open("rb") as handle:
            handle.seek(offset)
            length = int.from_bytes(handle.read(HEADER_BYTES), "big")
            handle.read(length)
            return handle.read(size)


def manifest_digest(labels: dict) -> str:
    canonical = json.dumps(labels, sort_keys=True, separators=(",", ":")).encode()
    return _sha256_bytes(canonical)


def write_manifest(path: Path, labels: dict) -> dict:
    payload = {"labels": labels}
    payload["sha256"] = manifest_digest(labels)
    path.write_text(json.dumps(payload, sort_keys=True))
    return payload


def restore_manifest(manifest: dict, store: PackStore, destination: Path) -> None:
    destination = Path(destination)
    for label, record in manifest["labels"].items():
        target = destination / label
        target.mkdir(parents=True, exist_ok=True)
        for relative, (digest, _size) in record["files"].items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(store.read_object(digest))
