import json
from typing import Any, Literal, NamedTuple, cast
from unittest import mock

import pytest
from conftest import TESTDIR, FsFactory
from googleapiclient.errors import HttpError

from gdrive_fsspec.core import (
    DIR_MIME_TYPE,
    GoogleDriveFile,
    GoogleDriveFileSystem,
    _finfo_from_response,
    _normalize_path,
)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prefix, name, expected",
    [
        ("/a/b/", "c", "/a/b/c"),
        ("a/b", "c", "/a/b/c"),
    ],
)
def test_normalize_path(prefix: str, name: str, expected: str) -> None:
    assert _normalize_path(prefix, name) == expected


@pytest.mark.parametrize(
    "mime_type, expected_type",
    [
        ("text/plain", "file"),
        (DIR_MIME_TYPE, "directory"),
    ],
)
def test_finfo_from_response_type(mime_type: str, expected_type: str) -> None:
    info = _finfo_from_response(
        {"name": "child", "mimeType": mime_type}, path_prefix="parent"
    )
    assert info["type"] == expected_type
    assert info["name"] == "parent/child"


def test_finfo_from_response_casts_size() -> None:
    assert _finfo_from_response({"name": "x", "size": "12"})["size"] == 12


def test_finfo_from_response_defaults_missing_size() -> None:
    assert _finfo_from_response({"name": "x"})["size"] == 0


def test_finfo_from_response_strips_leading_slash() -> None:
    info = _finfo_from_response({"name": "f"}, path_prefix="/top")
    assert info["name"] == "top/f"


# ---------------------------------------------------------------------------
# Construction and connection (no network)
# ---------------------------------------------------------------------------


def test_create_anon(anon_fs: GoogleDriveFileSystem) -> None:
    assert anon_fs.service is not None


def test_srv_is_deprecated_alias(anon_fs: GoogleDriveFileSystem) -> None:
    with pytest.warns(DeprecationWarning):
        assert anon_fs.srv is anon_fs.service


def test_auth_kwargs() -> None:
    fs = GoogleDriveFileSystem(
        token="anon",
        auth_kwargs={"user_email": "test@example.com"},
        skip_instance_cache=True,
    )
    assert fs.service is not None
    assert fs.auth_kwargs == {"user_email": "test@example.com"}


def test_connect_invalid_method() -> None:
    with pytest.raises(ValueError):
        GoogleDriveFileSystem(token=cast(Any, "bogus"), skip_instance_cache=True)


def test_invalid_access_raises() -> None:
    with pytest.raises(KeyError):
        GoogleDriveFileSystem(
            token="anon", access=cast(Any, "nope"), skip_instance_cache=True
        )


@pytest.mark.parametrize(
    "access, expected_scopes",
    [
        ("full_control", ["https://www.googleapis.com/auth/drive"]),
        ("read_only", ["https://www.googleapis.com/auth/drive.readonly"]),
    ],
)
def test_access_scopes_mapping(
    access: Literal["full_control", "read_only"],
    expected_scopes: list[str],
) -> None:
    fs = GoogleDriveFileSystem(token="anon", access=access, skip_instance_cache=True)
    assert fs.scopes == expected_scopes


def test_upload_chunk_without_parent_dircache() -> None:
    fs = GoogleDriveFileSystem(
        token="anon", skip_instance_cache=True, use_listings_cache=False
    )
    fs.files = mock.Mock()
    fs.files._http.request.return_value = (
        {"status": "200"},
        json.dumps({"id": "file-id", "name": "file.txt", "size": "4"}).encode(),
    )
    file = GoogleDriveFile(fs, "parent/file.txt", mode="wb")
    file.location = "https://example.invalid/upload?upload_id=1"
    file.write(b"data")
    file.offset = 0

    try:
        assert file._upload_chunk(final=True) is True
    finally:
        file.closed = True

    assert "parent" not in fs.dircache
    assert file.file_id == "file-id"


def test_drive_kw_without_drive(anon_fs: GoogleDriveFileSystem) -> None:
    assert anon_fs._drive_kw() == {}


def test_drive_kw_with_drive(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs.drive = "drive-123"
    kw = anon_fs._drive_kw()
    assert kw["driveId"] == "drive-123"
    assert kw["supportsAllDrives"] is True


def test_root_info(anon_fs: GoogleDriveFileSystem) -> None:
    info = anon_fs.info("")
    assert info["type"] == "directory"
    assert info["id"] == anon_fs.root_file_id


class MockedDriveFS(NamedTuple):
    fs: GoogleDriveFileSystem
    files: mock.Mock
    service: mock.Mock


@pytest.fixture()
def validation_fs(anon_fs: GoogleDriveFileSystem) -> MockedDriveFS:
    files = mock.Mock()
    service = mock.Mock()
    anon_fs.files = files
    anon_fs.service = service
    return MockedDriveFS(anon_fs, files, service)


def _http_error(status: int) -> HttpError:
    resp = mock.Mock(status=status, reason="Error")
    return HttpError(resp, b'{"error": {"message": "x"}}')


def test_validate_root_file_id_accepts_folder(validation_fs: MockedDriveFS) -> None:
    validation_fs.files.get.return_value.execute.return_value = {
        "id": "folder-id",
        "trashed": False,
        "mimeType": DIR_MIME_TYPE,
    }

    validation_fs.fs._validate_root_file_id("folder-id")

    validation_fs.files.get.assert_called_once_with(
        fileId="folder-id",
        fields="id,trashed,mimeType",
        supportsAllDrives=True,
    )


def test_validate_root_file_id_rejects_non_folder(
    validation_fs: MockedDriveFS,
) -> None:
    validation_fs.files.get.return_value.execute.return_value = {
        "id": "file-id",
        "trashed": False,
        "mimeType": "text/plain",
    }

    with pytest.raises(NotADirectoryError, match="not a folder"):
        validation_fs.fs._validate_root_file_id("file-id")


def test_validate_root_file_id_rejects_trashed_folder(
    validation_fs: MockedDriveFS,
) -> None:
    validation_fs.files.get.return_value.execute.return_value = {
        "id": "folder-id",
        "trashed": True,
        "mimeType": DIR_MIME_TYPE,
    }

    with pytest.raises(FileNotFoundError, match="trashed"):
        validation_fs.fs._validate_root_file_id("folder-id")


def test_validate_root_file_id_accepts_drive_id_and_sets_drive(
    validation_fs: MockedDriveFS,
) -> None:
    validation_fs.fs.drive = None
    validation_fs.files.get.return_value.execute.side_effect = _http_error(404)
    validation_fs.service.drives.return_value.get.return_value.execute.return_value = {
        "id": "drive-id",
    }

    validation_fs.fs._validate_root_file_id("drive-id")

    assert validation_fs.fs.drive == "drive-id"
    validation_fs.service.drives.return_value.get.assert_called_once_with(
        driveId="drive-id"
    )


def test_validate_root_file_id_drive_id_does_not_override_drive(
    validation_fs: MockedDriveFS,
) -> None:
    validation_fs.fs.drive = "existing-drive"
    validation_fs.files.get.return_value.execute.side_effect = _http_error(404)
    validation_fs.service.drives.return_value.get.return_value.execute.return_value = {
        "id": "drive-id",
    }

    validation_fs.fs._validate_root_file_id("drive-id")

    assert validation_fs.fs.drive == "existing-drive"


def test_validate_root_file_id_missing(validation_fs: MockedDriveFS) -> None:
    validation_fs.files.get.return_value.execute.side_effect = _http_error(404)
    validation_fs.service.drives.return_value.get.return_value.execute.side_effect = (
        _http_error(404)
    )

    with pytest.raises(FileNotFoundError, match="not found"):
        validation_fs.fs._validate_root_file_id("missing-id")


def test_validate_root_file_id_propagates_drive_permission_error(
    validation_fs: MockedDriveFS,
) -> None:
    validation_fs.files.get.return_value.execute.side_effect = _http_error(404)
    validation_fs.service.drives.return_value.get.return_value.execute.side_effect = (
        _http_error(403)
    )

    with pytest.raises(HttpError) as exc_info:
        validation_fs.fs._validate_root_file_id("drive-id")
    assert exc_info.value.status_code == 403


def test_ls_empty_root_returns_empty(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs._list_directory_by_id = mock.Mock(return_value=[])

    assert anon_fs.ls("") == []
    assert anon_fs.dircache[""] == []


def test_ls_missing_path_on_empty_root_raises(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    anon_fs._list_directory_by_id = mock.Mock(return_value=[])

    with pytest.raises(FileNotFoundError):
        anon_fs.ls("missing")


def test_invalidate_cache_path(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs.dircache["parent"] = [{"name": "parent/file"}]
    anon_fs.dircache["other"] = [{"name": "other/file"}]

    anon_fs.invalidate_cache("parent")

    assert "parent" not in anon_fs.dircache
    assert anon_fs.dircache["other"] == [{"name": "other/file"}]


def test_invalidate_cache_all(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs.dircache["parent"] = [{"name": "parent/file"}]
    anon_fs.dircache["other"] = [{"name": "other/file"}]

    anon_fs.invalidate_cache()

    assert anon_fs.dircache == {}


def test_drive_id_from_name_single_match(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs.drives = [{"id": "1", "name": "foo"}, {"id": "2", "name": "bar"}]
    assert anon_fs._drive_id_from_name("foo") == "1"


def test_drive_id_from_name_missing(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs.drives = [{"id": "1", "name": "foo"}]
    with pytest.raises(ValueError):
        anon_fs._drive_id_from_name("missing")


def test_drive_id_from_name_duplicate(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs.drives = [{"id": "1", "name": "dup"}, {"id": "2", "name": "dup"}]
    with pytest.raises(ValueError):
        anon_fs._drive_id_from_name("dup")


@pytest.mark.parametrize(
    "creds",
    [
        {"type": "service_account"},
        '{"type": "service_account"}',
    ],
)
def test_service_account_creds_parsing(creds: dict[str, Any] | str) -> None:
    target = "gdrive_fsspec.core.service_account.Credentials.from_service_account_info"
    with mock.patch(target) as from_info:
        GoogleDriveFileSystem(
            token="service_account",
            creds=creds,
            skip_instance_cache=True,
        )
    from_info.assert_called_once()
    assert from_info.call_args.kwargs["info"] == {"type": "service_account"}


@pytest.mark.parametrize("creds", ["", "   ", "\t\n"])
def test_service_account_empty_creds_raises(creds: str) -> None:
    with pytest.raises(ValueError, match="Empty credentials"):
        GoogleDriveFileSystem(
            token="service_account",
            creds=creds,
            skip_instance_cache=True,
        )


# ---------------------------------------------------------------------------
# Integration (require live Google Drive credentials)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_root_file_id_rejects_file(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """A regular file ID must not be accepted as the filesystem root."""
    fn = TESTDIR + "/root_is_a_file"
    with fs.open(fn, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"x")
    file_id = fs.info(fn)["id"]

    with pytest.raises(NotADirectoryError):
        make_fs(root_file_id=file_id)


@pytest.mark.integration
def test_root_file_id_accepts_folder(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """A folder ID is a valid root and lists its children from ``ls("")``."""
    folder = TESTDIR + "/root_folder"
    fs.mkdir(folder)
    with fs.open(folder + "/child", "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"data")
    folder_id = fs.info(folder)["id"]

    rooted = make_fs(root_file_id=folder_id)
    names = [item["name"] for item in rooted.ls("", detail=True)]
    assert "child" in names


@pytest.mark.integration
def test_simple(fs: GoogleDriveFileSystem) -> None:
    assert fs.ls("")
    data = b"hello"
    fn = TESTDIR + "/testfile"
    with fs.open(fn, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(data)
    assert fs.cat(fn) == data


@pytest.mark.integration
def test_create_directory(fs: GoogleDriveFileSystem) -> None:
    fs.makedirs(TESTDIR + "/data")
    fs.makedirs(TESTDIR + "/data/bar/baz")

    assert fs.exists(TESTDIR + "/data")
    assert fs.exists(TESTDIR + "/data/bar")
    assert fs.exists(TESTDIR + "/data/bar/baz")

    data = b"intermediate path"
    with fs.open(TESTDIR + "/data/bar/test", "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(data)
    assert fs.cat(TESTDIR + "/data/bar/test") == data
