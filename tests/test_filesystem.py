from __future__ import annotations

import os
from pathlib import Path

import pytest

import reviewctl.filesystem as filesystem


def test_confined_directory_fails_closed_without_platform_support(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(filesystem, "_OPEN_SUPPORTS_DIR_FD", False)

    with pytest.raises(OSError, match="cannot confine"):
        with filesystem.confined_directory_descriptor(tmp_path):
            pass


def test_confined_directory_rejects_missing_component_without_creation(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        with filesystem.confined_directory_descriptor(tmp_path / "missing"):
            pass


def test_confined_directory_keeps_non_macos_paths_unchanged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(filesystem.sys, "platform", "linux")

    assert filesystem._confined_absolute_directory_path(tmp_path) == tmp_path


def test_confined_directory_keeps_unaliased_macos_paths_unchanged(monkeypatch) -> None:
    path = Path("/Users/reviewctl/artifacts")
    monkeypatch.setattr(filesystem.sys, "platform", "darwin")

    assert filesystem._confined_absolute_directory_path(path) == path


def test_confined_directory_normalizes_only_the_macos_tmp_alias(
    tmp_path: Path, monkeypatch
) -> None:
    alias = tmp_path / "tmp"
    private_tmp = tmp_path / "private" / "tmp"
    private_tmp.mkdir(parents=True)
    alias.symlink_to(private_tmp, target_is_directory=True)
    monkeypatch.setattr(filesystem.sys, "platform", "darwin")
    monkeypatch.setattr(filesystem, "_MACOS_SYSTEM_ALIASES", {alias: private_tmp})

    with filesystem.confined_directory_descriptor(alias / "reviewctl", create=True) as descriptor:
        assert filesystem.stat.S_ISDIR(os.fstat(descriptor).st_mode)

    assert (private_tmp / "reviewctl").is_dir()


def test_confined_directory_still_rejects_a_symlink_below_macos_tmp(
    tmp_path: Path, monkeypatch
) -> None:
    alias = tmp_path / "tmp"
    private_tmp = tmp_path / "private" / "tmp"
    external = tmp_path / "external"
    private_tmp.mkdir(parents=True)
    external.mkdir()
    alias.symlink_to(private_tmp, target_is_directory=True)
    (private_tmp / "escape").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(filesystem.sys, "platform", "darwin")
    monkeypatch.setattr(filesystem, "_MACOS_SYSTEM_ALIASES", {alias: private_tmp})

    with pytest.raises(OSError):
        with filesystem.confined_directory_descriptor(alias / "escape" / "reviewctl", create=True):
            pass

    assert list(external.iterdir()) == []


def test_relative_confinement_rejects_unsupported_platform_and_invalid_paths(
    tmp_path: Path, monkeypatch
) -> None:
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match="relative path"):
            with filesystem.confined_relative_regular_descriptor(
                parent, Path("../escape"), os.O_RDONLY
            ):
                pass
        monkeypatch.setattr(filesystem, "_OPEN_SUPPORTS_DIR_FD", False)
        with pytest.raises(OSError, match="cannot confine"):
            with filesystem.confined_relative_directory_descriptor(parent, ()):
                pass
        with pytest.raises(OSError, match="cannot confine"):
            with filesystem.confined_relative_regular_descriptor(
                parent, Path("missing"), os.O_RDONLY
            ):
                pass
        with pytest.raises(OSError, match="cannot confine"):
            with filesystem.confined_regular_descriptor(tmp_path / "missing", os.O_RDONLY):
                pass
    finally:
        os.close(parent)


def test_relative_directory_validates_parent_missing_and_child_descriptors(
    tmp_path: Path, monkeypatch
) -> None:
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    file_path = tmp_path / "file"
    file_path.write_text("not a directory")
    file_descriptor = os.open(file_path, os.O_RDONLY)
    try:
        with pytest.raises(OSError, match="parent descriptor"):
            with filesystem.confined_relative_directory_descriptor(file_descriptor, ()):
                pass
        with pytest.raises(FileNotFoundError):
            with filesystem.confined_relative_directory_descriptor(parent, ("missing",)):
                pass
        child = tmp_path / "child"
        child.mkdir()
        real_is_directory = filesystem.stat.S_ISDIR
        calls = 0

        def reject_child(mode: int) -> bool:
            nonlocal calls
            calls += 1
            return calls == 1 and real_is_directory(mode)

        monkeypatch.setattr(filesystem.stat, "S_ISDIR", reject_child)
        with pytest.raises(OSError, match="path component"):
            with filesystem.confined_relative_directory_descriptor(parent, (child.name,)):
                pass
    finally:
        os.close(file_descriptor)
        os.close(parent)


def test_relative_directory_creates_missing_components(tmp_path: Path) -> None:
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with filesystem.confined_relative_directory_descriptor(
            parent, ("created", "nested"), create=True
        ) as descriptor:
            assert filesystem.stat.S_ISDIR(os.fstat(descriptor).st_mode)
    finally:
        os.close(parent)


def test_relative_regular_close_error_preserves_primary_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "receipt.json"
    path.write_text("{}")
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_open = filesystem.os.open
    real_close = filesystem.os.close
    regular_descriptor: int | None = None

    def tracked_open(open_path, flags, *args, **kwargs):
        nonlocal regular_descriptor
        descriptor = real_open(open_path, flags, *args, **kwargs)
        if Path(open_path) == Path(path.name) and kwargs.get("dir_fd") is not None:
            regular_descriptor = descriptor
        return descriptor

    def fail_regular_close(descriptor: int) -> None:
        if descriptor == regular_descriptor:
            real_close(descriptor)
            raise OSError("relative regular close failed")
        real_close(descriptor)

    monkeypatch.setattr(filesystem.os, "open", tracked_open)
    monkeypatch.setattr(filesystem.os, "close", fail_regular_close)
    try:
        with pytest.raises(RuntimeError, match="body failed"):
            with filesystem.confined_relative_regular_descriptor(
                parent, Path(path.name), os.O_RDONLY
            ):
                raise RuntimeError("body failed")
        with pytest.raises(OSError, match="relative regular close failed"):
            with filesystem.confined_relative_regular_descriptor(
                parent, Path(path.name), os.O_RDONLY
            ):
                pass
    finally:
        real_close(parent)


def test_confined_directory_close_error_does_not_replace_a_primary_error(
    tmp_path: Path, monkeypatch
) -> None:
    real_close = filesystem.os.close

    def fail_close(descriptor: int) -> None:
        try:
            raise OSError("close failed")
        finally:
            real_close(descriptor)

    monkeypatch.setattr(filesystem.os, "close", fail_close)

    with pytest.raises(RuntimeError, match="body failed"):
        with filesystem.confined_directory_descriptor(tmp_path):
            raise RuntimeError("body failed")
    with pytest.raises(OSError, match="close failed"):
        with filesystem.confined_directory_descriptor(tmp_path):
            pass


def test_confined_regular_close_error_does_not_replace_a_primary_error(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "receipt.json"
    path.write_text("{}")
    real_open = filesystem.os.open
    real_close = filesystem.os.close
    regular_descriptor: int | None = None
    close_failures = 0

    def tracked_open(open_path, flags, *args, **kwargs):
        nonlocal regular_descriptor
        descriptor = real_open(open_path, flags, *args, **kwargs)
        if Path(open_path) == Path(path.name) and kwargs.get("dir_fd") is not None:
            regular_descriptor = descriptor
        return descriptor

    def fail_regular_close(descriptor: int) -> None:
        nonlocal close_failures
        if descriptor == regular_descriptor:
            close_failures += 1
            real_close(descriptor)
            raise OSError("regular close failed")
        real_close(descriptor)

    monkeypatch.setattr(filesystem.os, "open", tracked_open)
    monkeypatch.setattr(filesystem.os, "close", fail_regular_close)

    with pytest.raises(RuntimeError, match="body failed"):
        with filesystem.confined_regular_descriptor(path, os.O_RDONLY):
            raise RuntimeError("body failed")
    with pytest.raises(OSError, match="regular close failed"):
        with filesystem.confined_regular_descriptor(path, os.O_RDONLY):
            pass
    assert close_failures == 2


def test_read_confined_rejects_sparse_oversize_before_stream_allocation(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "oversized-evidence"
    with path.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)

    def must_not_open_stream(*args, **kwargs):
        raise AssertionError("oversized descriptor must be rejected before reading")

    monkeypatch.setattr(filesystem.os, "fdopen", must_not_open_stream)
    with pytest.raises(OSError, match="byte limit"):
        filesystem.read_confined_bytes(path)


def test_read_confined_explicit_boundaries_and_text_limit(tmp_path: Path) -> None:
    path = tmp_path / "evidence"
    path.write_bytes(b"abc")
    assert filesystem.read_confined_bytes(path, max_bytes=3) == b"abc"
    assert filesystem.read_confined_text(path, max_bytes=3) == "abc"
    with pytest.raises(OSError, match="byte limit"):
        filesystem.read_confined_text(path, max_bytes=2)
    path.write_bytes(b"")
    assert filesystem.read_confined_bytes(path, max_bytes=0) == b""


@pytest.mark.parametrize("limit", [-1, None, True, 1.5, 2**100])
def test_read_confined_rejects_invalid_trusted_override_before_open(
    tmp_path: Path, limit
) -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        filesystem.read_confined_bytes(tmp_path / "absent", max_bytes=limit)


@pytest.mark.parametrize(
    "change", ["grow_before_read", "grow_within_limit", "grow_after_read", "shrink_after_read"]
)
def test_read_confined_detects_size_changes_and_never_requests_unbounded_read(
    tmp_path: Path, monkeypatch, change
) -> None:
    path = tmp_path / "mutable-evidence"
    path.write_bytes(b"abc")
    real_fdopen = filesystem.os.fdopen
    requested = []

    class ChangingStream:
        def __init__(self, descriptor, mode):
            self.stream = real_fdopen(descriptor, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size=-1):
            requested.append(size)
            assert size == 5, "read must always be bounded by max_bytes + 1"
            if change == "grow_before_read":
                with path.open("ab") as writer:
                    writer.truncate(32 * 1024 * 1024)
            if change == "grow_within_limit":
                with path.open("ab") as writer:
                    writer.write(b"x")
            data = self.stream.read(size)
            if change == "grow_after_read":
                with path.open("ab") as writer:
                    writer.truncate(32 * 1024 * 1024)
            elif change == "shrink_after_read":
                path.write_bytes(b"")
            return data

    monkeypatch.setattr(filesystem.os, "fdopen", ChangingStream)
    with pytest.raises(OSError, match="byte limit|changed while reading"):
        filesystem.read_confined_bytes(path, max_bytes=4)
    assert requested == [5]


def test_read_confined_rejects_fifo_and_symlink_without_blocking(tmp_path: Path) -> None:
    import subprocess

    regular = tmp_path / "regular"
    regular.write_bytes(b"safe")
    symlink = tmp_path / "symlink"
    symlink.symlink_to(regular)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    code = (
        "from pathlib import Path; from reviewctl.filesystem import read_confined_bytes; "
        "read_confined_bytes(Path(__import__('sys').argv[1]))"
    )
    for path in [fifo, symlink]:
        result = subprocess.run(
            [filesystem.sys.executable, "-c", code, str(path)],
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert result.returncode != 0
        assert b"OSError" in result.stderr
