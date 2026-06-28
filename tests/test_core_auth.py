"""Unit tests for GoogleDriveFileSystem authentication and construction."""

from pathlib import Path
from typing import Any
from unittest import mock

from gdrive_fsspec.core import ROOT_ID, GoogleDriveFileSystem


def test_anon_clears_drive() -> None:
    fs = GoogleDriveFileSystem(
        token="anon",
        drive="would-resolve",
        skip_instance_cache=True,
    )

    assert fs.drive is None


def test_init_resolves_drive_for_non_anon() -> None:
    target = "gdrive_fsspec.core.service_account.Credentials.from_service_account_info"
    with mock.patch(target, return_value=mock.Mock()):
        with mock.patch("gdrive_fsspec.core.build"):
            with mock.patch.object(
                GoogleDriveFileSystem,
                "_resolve_drive_id",
                return_value="resolved-id",
            ) as resolve:
                fs = GoogleDriveFileSystem(
                    token="service_account",
                    creds={"type": "service_account"},
                    drive="My Drive",
                    skip_instance_cache=True,
                )

    resolve.assert_called_once_with("My Drive")
    assert fs.drive == "resolved-id"


def test_init_validates_root_file_id() -> None:
    target = "gdrive_fsspec.core.service_account.Credentials.from_service_account_info"
    with mock.patch(target, return_value=mock.Mock()):
        with mock.patch("gdrive_fsspec.core.build"):
            with mock.patch.object(
                GoogleDriveFileSystem, "_validate_root_file_id"
            ) as validate:
                GoogleDriveFileSystem(
                    token="service_account",
                    creds={"type": "service_account"},
                    root_file_id="folder-id",
                    skip_instance_cache=True,
                )

    validate.assert_called_once_with("folder-id")


def test_init_skips_validation_for_default_root() -> None:
    target = "gdrive_fsspec.core.service_account.Credentials.from_service_account_info"
    with mock.patch(target, return_value=mock.Mock()):
        with mock.patch("gdrive_fsspec.core.build"):
            with mock.patch.object(
                GoogleDriveFileSystem, "_validate_root_file_id"
            ) as validate:
                GoogleDriveFileSystem(
                    token="service_account",
                    creds={"type": "service_account"},
                    root_file_id=ROOT_ID,
                    skip_instance_cache=True,
                )

    validate.assert_not_called()


def test_init_root_file_id_defaults_to_drive_or_root() -> None:
    target = "gdrive_fsspec.core.service_account.Credentials.from_service_account_info"
    with mock.patch(target, return_value=mock.Mock()):
        with mock.patch("gdrive_fsspec.core.build"):
            with mock.patch.object(
                GoogleDriveFileSystem,
                "_resolve_drive_id",
                return_value="drive-id",
            ):
                scoped = GoogleDriveFileSystem(
                    token="service_account",
                    creds={"type": "service_account"},
                    drive="team",
                    skip_instance_cache=True,
                )
                plain = GoogleDriveFileSystem(
                    token="anon",
                    skip_instance_cache=True,
                )

    assert scoped.root_file_id == "drive-id"
    assert plain.root_file_id == ROOT_ID


def test_connect_browser_removes_cache_before_cache_connect() -> None:
    fs = GoogleDriveFileSystem(token="anon", skip_instance_cache=True)
    cred = mock.Mock()

    with mock.patch("gdrive_fsspec.core.os.remove") as remove:
        with mock.patch.object(fs, "_connect_cache", return_value=cred) as cache:
            with mock.patch("gdrive_fsspec.core.build"):
                fs.connect(method="browser")

    remove.assert_called_once_with(fs._user_credentials_cache_path)
    cache.assert_called_once()
    assert fs.service is not None


def test_connect_browser_ignores_missing_cache_file() -> None:
    fs = GoogleDriveFileSystem(token="anon", skip_instance_cache=True)

    with mock.patch("gdrive_fsspec.core.os.remove", side_effect=FileNotFoundError):
        with mock.patch.object(fs, "_connect_cache", return_value=mock.Mock()):
            with mock.patch("gdrive_fsspec.core.build"):
                fs.connect(method="browser")


def test_connect_cache_passes_auth_kwargs() -> None:
    target = "pydata_google_auth.get_user_credentials"
    with mock.patch(target, return_value=mock.Mock()) as get_creds:
        with mock.patch("gdrive_fsspec.core.build"):
            GoogleDriveFileSystem(
                token="cache",
                auth_kwargs={"use_local_webserver": False},
                skip_instance_cache=True,
            )

    get_creds.assert_called_once_with(
        ["https://www.googleapis.com/auth/drive"],
        use_local_webserver=False,
    )


def test_service_account_creds_from_file(tmp_path: Path) -> None:
    cred_file = tmp_path / "sa.json"
    cred_file.write_text('{"type": "service_account", "project_id": "proj"}')
    target = "gdrive_fsspec.core.service_account.Credentials.from_service_account_info"

    with mock.patch(target) as from_info:
        with mock.patch("gdrive_fsspec.core.build"):
            GoogleDriveFileSystem(
                token="service_account",
                creds=str(cred_file),
                skip_instance_cache=True,
            )

    assert from_info.call_args.kwargs["info"] == {
        "type": "service_account",
        "project_id": "proj",
    }


def test_service_account_passes_auth_kwargs() -> None:
    target = "gdrive_fsspec.core.service_account.Credentials.from_service_account_info"
    creds: dict[str, Any] = {"type": "service_account"}

    with mock.patch(target) as from_info:
        with mock.patch("gdrive_fsspec.core.build"):
            GoogleDriveFileSystem(
                token="service_account",
                creds=creds,
                auth_kwargs={"quota_project_id": "my-project"},
                skip_instance_cache=True,
            )

    assert from_info.call_args.kwargs["quota_project_id"] == "my-project"
