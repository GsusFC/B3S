#!/usr/bin/env python3
"""Prepare fixed web-writable Vault volume directories without following links."""

from __future__ import annotations

import grp
import os
from pathlib import Path
import pwd
import stat
import sys


_DATA_ROOT = Path("/data")
_APPLICATION_DIRECTORIES = ("reports", "screenshots")
_DIRECTORY_MODE = 0o750


class VolumePreparationError(RuntimeError):
    pass


def prepare_volume(
    data_root: Path = _DATA_ROOT,
    *,
    web_uid: int,
    web_gid: int,
    root_uid: int = 0,
) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        root_fd = os.open(data_root, flags)
        try:
            root_info = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != root_uid
                or stat.S_IMODE(root_info.st_mode) != 0o1777
            ):
                raise ValueError("unsafe volume root")
            for name in _APPLICATION_DIRECTORIES:
                created = False
                try:
                    os.mkdir(name, _DIRECTORY_MODE, dir_fd=root_fd)
                    created = True
                except FileExistsError:
                    pass
                directory_fd = os.open(name, flags, dir_fd=root_fd)
                try:
                    info = os.fstat(directory_fd)
                    if not stat.S_ISDIR(info.st_mode):
                        raise ValueError("application path is not a directory")
                    if created:
                        os.fchown(directory_fd, web_uid, web_gid)
                        os.fchmod(directory_fd, _DIRECTORY_MODE)
                        info = os.fstat(directory_fd)
                    if info.st_uid != web_uid or info.st_gid != web_gid:
                        raise ValueError("application directory owner drift")
                    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                        raise ValueError("application directory is broadly writable")
                finally:
                    os.close(directory_fd)
        finally:
            os.close(root_fd)
    except Exception:
        raise VolumePreparationError("vault_volume_preparation_failed") from None


def main() -> int:
    if os.geteuid() != 0:
        print("vault volume preparation failed", file=sys.stderr)
        return 78
    try:
        web_uid = pwd.getpwnam("b3s").pw_uid
        web_gid = grp.getgrnam("b3s").gr_gid
        prepare_volume(web_uid=web_uid, web_gid=web_gid)
    except Exception:
        print("vault volume preparation failed", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
