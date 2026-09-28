"""Session workspace archiving: baseline snapshot + end-of-session archive.

Industrial closure for a sandboxed session: the workspace at turn end (already
synced back to the host by runner.close()) is archived as a tar.gz plus a
metadata json describing what this turn actually changed (baseline at turn
start vs. state at turn end).

Optional object-store upload (S3-compatible, e.g. MinIO) via PI_ARCHIVE_S3_*
settings; a pure no-op when unset, so the host archive always lands.

The host archive layout (archive_root = $PI_ARCHIVE_DIR or $HOME/.pi-py/archives)::

    <session_id>-<utc-ts>.tar.gz        workspace tarball
    <session_id>-<utc-ts>.json          metadata (below)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("pi.archive")

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
}

_MAX_FILES = 20_000
_SHA_CHUNK = 1 << 20


@dataclass
class WorkspaceFile:
    rel: str
    size: int
    sha1: str


@dataclass
class ArchiveRecord:
    session_id: str
    username: str
    archive_path: str
    ts: str
    files: int
    total_bytes: int
    diff: dict = field(default_factory=dict)
    s3: str | None = None
    s3_error: str | None = None

    def to_json(self) -> dict:
        return {
            "session_id": self.session_id,
            "username": self.username,
            "archive_ts": self.ts,
            "archive_path": self.archive_path,
            "files": self.files,
            "total_bytes": self.total_bytes,
            "diff": self.diff,
            "s3": self.s3,
            "s3_error": self.s3_error,
        }


def _sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(_SHA_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def snapshot_files(cwd: Path) -> list[WorkspaceFile]:
    """File manifest of a workspace (rel -> size+sha1), skipping build dirs."""
    out: list[WorkspaceFile] = []
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                out.append(
                    WorkspaceFile(
                        rel=str(p.relative_to(cwd)).replace("\\", "/"),
                        size=p.stat().st_size,
                        sha1=_sha1_file(p),
                    )
                )
            except OSError:
                continue
            if len(out) >= _MAX_FILES:
                return out
    return out


def compute_diff(before: list[WorkspaceFile], after: list[WorkspaceFile]) -> dict:
    """What this turn changed: added / modified / deleted / unchanged."""
    before_map = {f.rel: f for f in before}
    after_map = {f.rel: f for f in after}
    added = [
        f for rel, f in sorted(after_map.items()) if rel not in before_map
    ]
    modified = [
        f
        for rel, f in sorted(after_map.items())
        if rel in before_map and before_map[rel].sha1 != f.sha1
    ]
    deleted = sorted(rel for rel in before_map if rel not in after_map)
    unchanged = sum(1 for rel, f in before_map.items() if rel in after_map and after_map[rel].sha1 == f.sha1)
    return {
        "added": [f.rel for f in added],
        "modified": [f.rel for f in modified],
        "deleted": deleted,
        "unchanged": unchanged,
    }


def _archive_root() -> Path:
    return Path(
        os.environ.get("PI_ARCHIVE_DIR") or Path.home() / ".pi-py" / "archives"
    )


def enabled() -> bool:
    """Archiving is on by default; PI_ARCHIVE=0 disables it."""
    return os.environ.get("PI_ARCHIVE", "1").strip() not in ("0", "false", "no")


def _upload_s3(record: ArchiveRecord, tar_path: Path) -> tuple[bool, str | None]:
    """No-op unless PI_ARCHIVE_S3_ENDPOINT is configured (boto3 required)."""
    endpoint = os.environ.get("PI_ARCHIVE_S3_ENDPOINT", "").strip()
    if not endpoint:
        return False, None
    bucket = os.environ.get("PI_ARCHIVE_S3_BUCKET", "").strip()
    access = os.environ.get("PI_ARCHIVE_S3_ACCESS_KEY", "").strip()
    secret = os.environ.get("PI_ARCHIVE_S3_SECRET_KEY", "").strip()
    if not (bucket and access and secret):
        return False, "S3 configured but missing bucket/keys"
    try:
        import boto3  # noqa: PLC0415 - optional dependency

        key = f"pi-py-workspaces/{record.session_id}/{Path(tar_path).name}"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access,
            aws_secret_access_key=secret,
        )
        client.upload_file(str(tar_path), bucket, key)
        return True, f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001 - archiving must never break a turn
        log.warning("archive s3 upload failed", exc_info=True)
        return False, str(exc)


def archive_workspace(
    *,
    session_id: str,
    username: str,
    cwd: Path,
    baseline: list[WorkspaceFile] | None = None,
) -> ArchiveRecord | None:
    """Archive the workspace at turn end. Returns None when disabled or the
    directory is missing. Pure host-side unless S3 is configured."""
    root = _archive_root()
    if not (cwd.exists() and cwd.is_dir()):
        return None
    try:
        root.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stem = f"{session_id}-{ts}"
        tar_path = root / f"{stem}.tar.gz"

        def _exclude(info):
            base = info.name.split("/")[-1]
            if base in SKIP_DIRS or "/.git/" in f"/{info.name}/":
                return None
            return info

        with tarfile.open(tar_path, mode="w:gz", compresslevel=1) as tf:
            tf.add(cwd, arcname=".", recursive=True, filter=_exclude)

        after = snapshot_files(cwd)
        record = ArchiveRecord(
            session_id=session_id,
            username=username,
            archive_path=str(tar_path),
            ts=ts,
            files=len(after),
            total_bytes=sum(f.size for f in after),
            diff=compute_diff(baseline or [], after),
        )
        ok, s3_ref = _upload_s3(record, tar_path)
        if ok:
            record.s3 = s3_ref
        elif s3_ref:
            record.s3_error = s3_ref

        (root / f"{stem}.json").write_text(
            json.dumps(record.to_json(), ensure_ascii=False, indent=2)
        )
        log.info(
            "workspace archived session=%s files=%d bytes=%d diff=%s",
            session_id, record.files, record.total_bytes,
            {k: len(v) if isinstance(v, list) else v for k, v in record.diff.items()},
        )
        return record
    except Exception:  # noqa: BLE001 - archiving must never break a turn
        log.warning("workspace archive failed", exc_info=True)
        return None