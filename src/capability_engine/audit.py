"""Immutable SHA-256 hash-chained audit ledger and atomic private file helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

try:
    from src.core.private_storage import (
        open_private_text,
        restrictive_umask,
        secure_private_directory,
        secure_private_file,
    )
except ImportError:  # Standalone mode without src.core
    @contextmanager
    def restrictive_umask(mask: int = 0o077) -> Iterator[None]:
        old = os.umask(mask)
        try:
            yield
        finally:
            os.umask(old)

    def secure_private_directory(path: Path) -> Path:
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
        return target

    def secure_private_file(path: Path) -> Path:
        target = Path(path)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target

    @contextmanager
    def open_private_text(path: Path, mode: str = "a") -> Iterator[Any]:
        target = Path(path)
        secure_private_directory(target.parent)
        with restrictive_umask():
            handle = open(target, mode, encoding="utf-8")
            try:
                secure_private_file(target)
                yield handle
            finally:
                handle.close()

from .models import AuditEntry

_GENESIS_PREV_HASH = "0" * 64


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _compute_entry_hash(
    *,
    seq: int,
    timestamp_utc: str,
    actor: str,
    action: str,
    target_id: str,
    target_kind: str,
    digest: str,
    details: Dict[str, Any],
    prev_hash: str,
) -> str:
    body = {
        "seq": seq,
        "timestamp_utc": timestamp_utc,
        "actor": actor,
        "action": action,
        "target_id": target_id,
        "target_kind": target_kind,
        "digest": digest,
        "details": details,
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def atomic_write_private_json(path: Path, payload: Any) -> None:
    """Atomically write JSON with 0700 directory and 0600 file permissions."""
    target = Path(path)
    if target.is_symlink():
        raise PermissionError(f"Capability state file must not be a symlink: {target}")
    secure_private_directory(target.parent)
    encoded = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    with restrictive_umask():
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, target)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
    secure_private_file(target)


class CapabilityAuditLedger:
    """Append-only SHA-256 hash-chained audit trail for all capability lifecycle events."""

    def __init__(self, storage_path: Optional[Path] = None) -> None:
        self.storage_path: Optional[Path] = Path(storage_path) if storage_path is not None else None
        self._lock = threading.RLock()
        self._entries: list[AuditEntry] = []
        self._last_hash: str = _GENESIS_PREV_HASH
        if self.storage_path is not None:
            secure_private_directory(self.storage_path.parent)
            if self.storage_path.exists():
                secure_private_file(self.storage_path)
                self._load_existing()

    def _load_existing(self) -> None:
        assert self.storage_path is not None
        raw_lines = self.storage_path.read_text(encoding="utf-8").splitlines()
        loaded: list[AuditEntry] = []
        for raw in raw_lines:
            text = raw.strip()
            if not text:
                continue
            try:
                data = json.loads(text)
                entry = AuditEntry(
                    seq=int(data["seq"]),
                    timestamp_utc=str(data["timestamp_utc"]),
                    actor=str(data["actor"]),
                    action=str(data["action"]),
                    target_id=str(data["target_id"]),
                    target_kind=str(data["target_kind"]),
                    digest=str(data.get("digest", "")),
                    details=dict(data.get("details") or {}),
                    prev_hash=str(data["prev_hash"]),
                    entry_hash=str(data["entry_hash"]),
                )
                loaded.append(entry)
            except Exception:
                # Preserve sequence cursor; verify_integrity() re-validates raw file lines.
                loaded.append(
                    AuditEntry(
                        seq=len(loaded) + 1,
                        timestamp_utc="",
                        actor="corrupt",
                        action="corrupt",
                        target_id="corrupt",
                        target_kind="corrupt",
                        digest="",
                        details={},
                        prev_hash="corrupt",
                        entry_hash="corrupt",
                    )
                )
        self._entries = loaded
        self._last_hash = loaded[-1].entry_hash if loaded else _GENESIS_PREV_HASH

    def record(
        self,
        actor: str,
        action: str,
        target_id: str,
        *,
        target_kind: str = "capability",
        digest: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> AuditEntry:
        """Append one tamper-evident hash-chained audit entry."""
        clean_details = dict(details or {})
        with self._lock:
            seq = len(self._entries) + 1
            timestamp_utc = datetime.now(timezone.utc).isoformat()
            prev_hash = self._last_hash
            entry_hash = _compute_entry_hash(
                seq=seq,
                timestamp_utc=timestamp_utc,
                actor=str(actor),
                action=str(action),
                target_id=str(target_id),
                target_kind=str(target_kind),
                digest=str(digest or ""),
                details=clean_details,
                prev_hash=prev_hash,
            )
            entry = AuditEntry(
                seq=seq,
                timestamp_utc=timestamp_utc,
                actor=str(actor),
                action=str(action),
                target_id=str(target_id),
                target_kind=str(target_kind),
                digest=str(digest or ""),
                details=clean_details,
                prev_hash=prev_hash,
                entry_hash=entry_hash,
            )
            if self.storage_path is not None:
                line = _canonical_json(
                    {
                        "seq": entry.seq,
                        "timestamp_utc": entry.timestamp_utc,
                        "actor": entry.actor,
                        "action": entry.action,
                        "target_id": entry.target_id,
                        "target_kind": entry.target_kind,
                        "digest": entry.digest,
                        "details": entry.details,
                        "prev_hash": entry.prev_hash,
                        "entry_hash": entry.entry_hash,
                    }
                )
                with open_private_text(self.storage_path, "a") as handle:
                    handle.write(line + "\n")
                    handle.flush()
            self._entries.append(entry)
            self._last_hash = entry_hash
            return entry

    def entries(self, limit: int = 100) -> list[AuditEntry]:
        """Return newest audit entries up to `limit`."""
        with self._lock:
            if limit <= 0:
                return []
            return list(self._entries[-limit:])

    def verify_integrity(self) -> tuple[bool, list[str]]:
        """Recompute hashes from genesis and detect missing links or modifications."""
        reasons: list[str] = []
        with self._lock:
            if self.storage_path is not None and self.storage_path.exists():
                records = self._parse_disk_records(reasons)
            else:
                records = [
                    {
                        "seq": e.seq,
                        "timestamp_utc": e.timestamp_utc,
                        "actor": e.actor,
                        "action": e.action,
                        "target_id": e.target_id,
                        "target_kind": e.target_kind,
                        "digest": e.digest,
                        "details": e.details,
                        "prev_hash": e.prev_hash,
                        "entry_hash": e.entry_hash,
                    }
                    for e in self._entries
                ]
            if reasons:
                return False, reasons
            expected_prev = _GENESIS_PREV_HASH
            for idx, item in enumerate(records, start=1):
                if int(item.get("seq", -1)) != idx:
                    reasons.append(f"seq_mismatch:line_{idx}")
                if item.get("prev_hash") != expected_prev:
                    reasons.append(f"prev_hash_mismatch:line_{idx}")
                recomputed = _compute_entry_hash(
                    seq=int(item["seq"]),
                    timestamp_utc=str(item["timestamp_utc"]),
                    actor=str(item["actor"]),
                    action=str(item["action"]),
                    target_id=str(item["target_id"]),
                    target_kind=str(item["target_kind"]),
                    digest=str(item.get("digest", "")),
                    details=dict(item.get("details") or {}),
                    prev_hash=str(item["prev_hash"]),
                )
                if item.get("entry_hash") != recomputed:
                    reasons.append(f"entry_hash_mismatch:line_{idx}")
                expected_prev = str(item.get("entry_hash", ""))
            return len(reasons) == 0, reasons

    def _parse_disk_records(self, reasons: list[str]) -> list[Dict[str, Any]]:
        assert self.storage_path is not None
        parsed: list[Dict[str, Any]] = []
        for idx, raw in enumerate(self.storage_path.read_text(encoding="utf-8").splitlines(), start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                reasons.append(f"invalid_json:line_{idx}")
                continue
            required = {"seq", "timestamp_utc", "actor", "action", "target_id", "target_kind", "prev_hash", "entry_hash"}
            if not isinstance(obj, dict) or not required.issubset(obj.keys()):
                reasons.append(f"missing_fields:line_{idx}")
                continue
            parsed.append(obj)
        return parsed
