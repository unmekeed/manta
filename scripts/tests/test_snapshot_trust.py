"""Граница доверия слепка: layout + manifest + SHA-256 (спринт 158)."""

import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SYNC = ROOT / "scripts" / "dataset-sync.sh"


def _manifest(files: dict[str, bytes]) -> bytes:
    return "".join(
        f"{hashlib.sha256(data).hexdigest()}  ./{name}\n"
        for name, data in sorted(files.items())
    ).encode()


def archive(tmp_path: Path, files=None, *, manifest=True, extra_members=()):
    files = dict(files or {"meta.json": b'{"schema": 158}\n'})
    if manifest:
        files["MANIFEST.sha256"] = _manifest(files)
    path = tmp_path / "snapshot.tar"
    with tarfile.open(path, "w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for info, data in extra_members:
            tf.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def verify(path: Path):
    return subprocess.run(
        ["bash", str(SYNC), "verify", str(path)], cwd=ROOT,
        capture_output=True, text=True,
        env={**os.environ, "PATH": os.environ["PATH"]})


def test_valid_minimal_snapshot_passes(tmp_path):
    proc = verify(archive(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "слепок проверен" in proc.stdout


def test_manifest_is_required(tmp_path):
    proc = verify(archive(tmp_path, manifest=False))
    assert proc.returncode != 0
    assert "MANIFEST.sha256" in proc.stderr


def test_changed_payload_is_rejected(tmp_path):
    files = {"meta.json": b'{"schema": 158}\n'}
    bad = {**files, "MANIFEST.sha256": _manifest(files)}
    bad["meta.json"] = b'{"schema": 999}\n'
    proc = verify(archive(tmp_path, bad, manifest=False))
    assert proc.returncode != 0
    assert "SHA-256" in proc.stderr


def test_file_missing_from_manifest_is_rejected(tmp_path):
    files = {"meta.json": b"{}\n", "collectedmatches.cols": b"match_id\n"}
    files["MANIFEST.sha256"] = _manifest({"meta.json": files["meta.json"]})
    proc = verify(archive(tmp_path, files, manifest=False))
    assert proc.returncode != 0
    assert "manifest не совпадает" in proc.stderr


def test_unknown_file_is_rejected_even_with_valid_hash(tmp_path):
    files = {"meta.json": b"{}\n", "payload.sh": b"touch /tmp/pwned\n"}
    proc = verify(archive(tmp_path, files))
    assert proc.returncode != 0
    assert "лишний файл" in proc.stderr


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside", "dir/meta.json"])
def test_unsafe_or_nested_path_is_rejected(tmp_path, name):
    info = tarfile.TarInfo(name)
    data = b"bad"
    info.size = len(data)
    proc = verify(archive(tmp_path, extra_members=[(info, data)]))
    assert proc.returncode != 0
    assert "опасный путь" in proc.stderr


def test_symlink_is_rejected(tmp_path):
    info = tarfile.TarInfo("collectedmatches.cols")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    proc = verify(archive(tmp_path, extra_members=[(info, None)]))
    assert proc.returncode != 0
    assert "обычные файлы" in proc.stderr


def test_duplicate_member_is_rejected(tmp_path):
    info = tarfile.TarInfo("meta.json")
    data = b"duplicate"
    info.size = len(data)
    proc = verify(archive(tmp_path, extra_members=[(info, data)]))
    assert proc.returncode != 0
    assert "дублирующийся" in proc.stderr


def test_import_extracts_without_archive_owner_or_permissions():
    src = SYNC.read_text(encoding="utf-8")
    body = src[src.index("import_dataset()"):]
    assert "--no-same-owner" in body
    assert "--no-same-permissions" in body


def test_export_creates_manifest_before_tar():
    src = SYNC.read_text(encoding="utf-8")
    body = src[src.index("export_dataset()"):src.index("is_empty_dump()")]
    assert 'sha256sum "$f"' in body
    assert body.index('>"$dir/$MANIFEST"') < body.index('tar -cf "$out"')
