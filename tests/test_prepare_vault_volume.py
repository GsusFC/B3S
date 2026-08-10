from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from deploy import prepare_vault_volume


def _metadata(path: Path) -> tuple[int, ...]:
    info = path.stat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_gid,
        stat.S_IMODE(info.st_mode),
        info.st_size,
    )


def test_prepare_volume_creates_only_fd_relative_application_directories(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o1777)

    prepare_vault_volume.prepare_volume(
        tmp_path,
        web_uid=os.geteuid(),
        web_gid=os.getegid(),
        root_uid=os.geteuid(),
    )

    assert {path.name for path in tmp_path.iterdir()} == {"reports", "screenshots"}
    for name in ("reports", "screenshots"):
        info = (tmp_path / name).stat()
        assert stat.S_ISDIR(info.st_mode)
        assert info.st_uid == os.geteuid()
        assert info.st_gid == os.getegid()
        assert stat.S_IMODE(info.st_mode) == 0o750


@pytest.mark.parametrize("name", ["reports", "screenshots"])
def test_prepare_volume_rejects_symlink_without_touching_target(
    tmp_path: Path,
    name: str,
) -> None:
    tmp_path.chmod(0o1777)
    secret_directory = tmp_path / "b3s-vault-worker"
    secret_directory.mkdir(mode=0o700)
    secret_file = secret_directory / "private-key.b64"
    secret_file.write_text("do-not-touch")
    secret_file.chmod(0o600)
    before_directory = _metadata(secret_directory)
    before_file = _metadata(secret_file)
    (tmp_path / name).symlink_to(secret_directory, target_is_directory=True)

    with pytest.raises(
        prepare_vault_volume.VolumePreparationError,
        match="vault_volume_preparation_failed",
    ):
        prepare_vault_volume.prepare_volume(
            tmp_path,
            web_uid=os.geteuid(),
            web_gid=os.getegid(),
            root_uid=os.geteuid(),
        )

    assert _metadata(secret_directory) == before_directory
    assert _metadata(secret_file) == before_file


def test_entrypoint_never_chowns_web_controlled_volume_children() -> None:
    root = Path(__file__).resolve().parents[1]
    entrypoint = (root / "deploy" / "fly_entrypoint.sh").read_text()

    assert "chown root:root /data" in entrypoint
    assert "prepare_vault_volume.py" in entrypoint
    assert "chown -R" not in entrypoint
    assert "application_file" not in entrypoint
    assert "/data/brand3.sqlite3" not in entrypoint
    assert "/data/b3s_scoring.sqlite3" not in entrypoint
