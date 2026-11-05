"""Descriptor-confined filesystem access for review evidence and project state."""

from __future__ import annotations

import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

DEFAULT_MAX_READ_BYTES = 64 * 1024 * 1024

_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_MACOS_SYSTEM_ALIASES = {
    Path("/etc"): Path("/private/etc"),
    Path("/tmp"): Path("/private/tmp"),
    Path("/var"): Path("/private/var"),
}


def _close_descriptors(descriptors: list[int]) -> None:
    primary = sys.exc_info()[1]
    close_error: BaseException | None = None
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except BaseException as error:
            if primary is None and close_error is None:
                close_error = error
    if primary is None and close_error is not None:
        raise close_error


def _confined_absolute_directory_path(path: Path) -> Path:
    """Canonicalize macOS system aliases without resolving user-controlled symlinks."""
    absolute = Path(os.path.abspath(path.expanduser()))
    if sys.platform != "darwin":
        return absolute
    for alias, target in _MACOS_SYSTEM_ALIASES.items():
        try:
            return target / absolute.relative_to(alias)
        except ValueError:
            continue
    return absolute


@contextmanager
def confined_directory_descriptor(
    path: Path,
    *,
    create: bool = False,
    expected_identity: tuple[int, int] | None = None,
):
    """Open an absolute directory without following any pathname component."""
    absolute = _confined_absolute_directory_path(path)
    anchor = Path(absolute.anchor)
    parts = absolute.relative_to(anchor).parts
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_flag is None or not _OPEN_SUPPORTS_DIR_FD:
        raise OSError("this platform cannot confine filesystem paths")
    flags = os.O_RDONLY | directory_flag | no_follow
    descriptors: list[int] = []
    try:
        current = os.open(anchor, flags)
        descriptors.append(current)
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            raise OSError("filesystem anchor is not a directory")
        for component in parts:
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=current)
                child = os.open(component, flags, dir_fd=current)
            descriptors.append(child)
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                raise OSError("filesystem path component is not a directory")
            current = child
        metadata = os.fstat(current)
        if (
            expected_identity is not None
            and (metadata.st_dev, metadata.st_ino) != expected_identity
        ):
            raise OSError("filesystem directory identity changed")
        yield current
    finally:
        _close_descriptors(descriptors)


@contextmanager
def confined_relative_directory_descriptor(
    parent_descriptor: int,
    parts: tuple[str, ...],
    *,
    create: bool = False,
):
    """Walk relative directory components from an already validated descriptor."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_flag is None or not _OPEN_SUPPORTS_DIR_FD:
        raise OSError("this platform cannot confine filesystem paths")
    flags = os.O_RDONLY | directory_flag | no_follow
    descriptors = [os.dup(parent_descriptor)]
    current = descriptors[0]
    try:
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            raise OSError("filesystem parent descriptor is not a directory")
        for component in parts:
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=current)
                child = os.open(component, flags, dir_fd=current)
            descriptors.append(child)
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                raise OSError("filesystem path component is not a directory")
            current = child
        yield current
    finally:
        _close_descriptors(descriptors)


@contextmanager
def confined_relative_regular_descriptor(
    parent_descriptor: int,
    path: Path,
    flags: int,
    mode: int = 0o600,
):
    """Open a relative regular file below an already validated directory descriptor."""
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        raise ValueError("filesystem path must be a confined relative path")
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None or not _OPEN_SUPPORTS_DIR_FD:
        raise OSError("this platform cannot confine filesystem paths")
    with confined_relative_directory_descriptor(parent_descriptor, path.parts[:-1]) as parent:
        descriptor = os.open(
            path.parts[-1],
            flags | no_follow | getattr(os, "O_NONBLOCK", 0),
            mode,
            dir_fd=parent,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("filesystem path is not a regular file")
            yield descriptor
        finally:
            primary = sys.exc_info()[1]
            try:
                os.close(descriptor)
            except BaseException as close_error:
                if primary is not None:
                    raise primary from close_error
                raise


@contextmanager
def confined_regular_descriptor(
    path: Path,
    flags: int,
    mode: int = 0o600,
    *,
    expected_parent_identity: tuple[int, int] | None = None,
):
    """Open a regular file relative to a fully confined parent directory."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None or not _OPEN_SUPPORTS_DIR_FD:
        raise OSError("this platform cannot confine filesystem paths")
    with confined_directory_descriptor(
        path.parent, expected_identity=expected_parent_identity
    ) as parent:
        descriptor = os.open(
            path.name,
            flags | no_follow | getattr(os, "O_NONBLOCK", 0),
            mode,
            dir_fd=parent,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("filesystem path is not a regular file")
            yield descriptor
        finally:
            primary = sys.exc_info()[1]
            try:
                os.close(descriptor)
            except BaseException as close_error:
                if primary is not None:
                    raise primary from close_error
                raise


def read_confined_bytes(
    path: Path,
    *,
    expected_parent_identity: tuple[int, int] | None = None,
    max_bytes: int = DEFAULT_MAX_READ_BYTES,
) -> bytes:
    """Read a bounded regular file; only trusted callers may override the limit."""
    if type(max_bytes) is not int or not 0 <= max_bytes < sys.maxsize:
        raise ValueError("max_bytes must be a nonnegative bounded integer")
    with confined_regular_descriptor(
        path,
        os.O_RDONLY,
        expected_parent_identity=expected_parent_identity,
    ) as descriptor:
        before = os.fstat(descriptor).st_size
        if before > max_bytes:
            raise OSError("confined file exceeds byte limit")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            data = stream.read(max_bytes + 1)
        after = os.fstat(descriptor).st_size
        if len(data) > max_bytes or after > max_bytes:
            raise OSError("confined file exceeds byte limit")
        if len(data) != before or after != before:
            raise OSError("confined file changed while reading")
        return data


def read_confined_text(
    path: Path,
    *,
    expected_parent_identity: tuple[int, int] | None = None,
    max_bytes: int = DEFAULT_MAX_READ_BYTES,
) -> str:
    """Read bounded UTF-8 text from a confined regular-file descriptor."""
    return read_confined_bytes(
        path, expected_parent_identity=expected_parent_identity, max_bytes=max_bytes
    ).decode("utf-8")
