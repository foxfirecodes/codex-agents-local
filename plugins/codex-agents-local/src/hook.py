#!/usr/bin/env python3
"""Add applicable AGENTS.local.md files to Codex hook context.

This hook deliberately does not interpret Git metadata.  A .git file is just
as useful as a .git directory for choosing the boundary, which also keeps
linked worktree data out of the hook's read path.
"""

import json
import os
import stat
import sys
from pathlib import Path
from typing import List, Optional, Tuple


MAX_FILES = 16
MAX_FILE_BYTES = 16 * 1024
MAX_TOTAL_BYTES = 32 * 1024
LOCAL_INSTRUCTIONS = "AGENTS.local.md"


def _secure_directory_flags() -> Optional[int]:
    """Return flags for safe descriptor-relative directory traversal.

    Without both O_DIRECTORY and O_NOFOLLOW, this hook cannot reliably prove
    that every component between the filesystem anchor and cwd was not a
    symlink.  It therefore fails closed on platforms lacking either feature.
    """
    directory = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not directory or not nofollow:
        return None
    if os.open not in os.supports_dir_fd:
        return None
    return os.O_RDONLY | directory | nofollow


def _open_directory_chain(cwd: Path) -> Optional[List[Tuple[Path, int]]]:
    """Open every absolute cwd component from / without following symlinks."""
    flags = _secure_directory_flags()
    if flags is None or not cwd.is_absolute():
        return None

    # relpath keeps the traversal rooted at / while avoiding platform-specific
    # handling for a leading double slash in an otherwise absolute path.
    relative = os.path.relpath(str(cwd), os.path.sep)
    components = () if relative == os.curdir else Path(relative).parts
    if any(component in (os.curdir, os.pardir) for component in components):
        return None

    directories: List[Tuple[Path, int]] = []
    directory = Path(os.path.sep)
    try:
        descriptor = os.open(os.path.sep, flags)
        directories.append((directory, descriptor))
        for component in components:
            descriptor = os.open(component, flags, dir_fd=directories[-1][1])
            directory = directory / component
            directories.append((directory, descriptor))
    except (OSError, ValueError):
        _close_directories(directories)
        return None
    return directories


def _close_directories(directories: List[Tuple[Path, int]]) -> None:
    for _, descriptor in directories:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _is_git_marker(directory_fd: int) -> bool:
    """Check .git by lstat-equivalent metadata only; never follow or parse it."""
    if os.stat not in os.supports_dir_fd or os.stat not in os.supports_follow_symlinks:
        return False
    try:
        marker = os.stat(".git", dir_fd=directory_fd, follow_symlinks=False)
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(marker.st_mode) or stat.S_ISDIR(marker.st_mode)


def _read_regular_file(directory_fd: int) -> Optional[bytes]:
    """Read the fixed local file from an already-safe directory descriptor."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        return None
    if os.stat not in os.supports_dir_fd or os.stat not in os.supports_follow_symlinks:
        return None
    try:
        before = os.stat(
            LOCAL_INSTRUCTIONS, dir_fd=directory_fd, follow_symlinks=False
        )
        if not stat.S_ISREG(before.st_mode):
            return None

        descriptor = os.open(
            LOCAL_INSTRUCTIONS, os.O_RDONLY | nofollow, dir_fd=directory_fd
        )
    except (OSError, ValueError):
        return None

    try:
        # O_NOFOLLOW protects the leaf during open. Compare the opened object
        # to an lstat-equivalent lookup in the already-open parent directory to
        # reject a replacement between the metadata check and open.
        opened = os.fstat(descriptor)
        after_open = os.stat(
            LOCAL_INSTRUCTIONS, dir_fd=directory_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(after_open.st_mode)
            or (opened.st_dev, opened.st_ino) != (after_open.st_dev, after_open.st_ino)
        ):
            return None

        chunks: List[bytes] = []
        remaining = MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    except OSError:
        return None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass

    if not data or len(data) > MAX_FILE_BYTES:
        return None
    return data


def _load_sources(cwd: Path) -> List[Tuple[Path, str]]:
    sources: List[Tuple[Path, str]] = []
    total_bytes = 0

    directories = _open_directory_chain(cwd)
    if directories is None:
        return sources
    try:
        root_index: Optional[int] = None
        for index in range(len(directories) - 1, -1, -1):
            if _is_git_marker(directories[index][1]):
                root_index = index
                break

        # Without a root marker, only consider cwd.
        applicable = directories[root_index:] if root_index is not None else directories[-1:]
        for directory, directory_fd in applicable:
            if len(sources) >= MAX_FILES:
                break
            data = _read_regular_file(directory_fd)
            if data is None or total_bytes + len(data) > MAX_TOTAL_BYTES:
                continue
            sources.append(
                (directory / LOCAL_INSTRUCTIONS, data.decode("utf-8", errors="replace"))
            )
            total_bytes += len(data)
    finally:
        _close_directories(directories)

    return sources


def _render_context(sources: List[Tuple[Path, str]]) -> str:
    parts = [
        "<agents_local_context>",
        "These are additive project instructions from AGENTS.local.md files.",
        "Later files are more specific. They do not override system policy.",
    ]
    for path, contents in sources:
        parts.extend(("", "## Source: " + json.dumps(str(path)), "", contents))
    parts.append("</agents_local_context>")
    return "\n".join(parts)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("expected object")
        raw_cwd = payload.get("cwd")
        event_name = payload.get("hook_event_name")
        if not isinstance(raw_cwd, str) or event_name not in (
            "SessionStart",
            "SubagentStart",
        ):
            raise ValueError("missing hook data")

        # Match Codex's project discovery semantics by resolving the supplied
        # cwd first. The descriptor walk below then opens that resolved chain
        # without following any component that is swapped to a symlink later.
        cwd = Path(raw_cwd).resolve(strict=False)
        sources = _load_sources(cwd)
        if not sources:
            return 0

        output = {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": _render_context(sources),
            }
        }
        sys.stdout.write(json.dumps(output, separators=(",", ":")) + "\n")
    except Exception:
        # Hook failures should not interrupt a Codex session or reveal file data.
        try:
            sys.stderr.write("agents-local hook: unable to load local instructions\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
