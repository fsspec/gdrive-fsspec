"""Unit tests for GoogleDriveFileSystem directory and path operations."""

from typing import Any
from unittest import mock

import pytest
from conftest import MockedDriveFS, empty_files_list_response, empty_listing

from gdrive_fsspec.core import (
    DIR_MIME_TYPE,
    ROOT_ID,
    GoogleDriveFile,
    GoogleDriveFileSystem,
    MultipleFilesError,
)


def test_mkdir_creates_folder_and_updates_dircache(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.exists = mock.Mock(return_value=False)
    fs.info = mock.Mock(return_value={"id": "parent-id"})
    mocked_fs.files.create.return_value.execute.return_value = {
        "id": "new-id",
        "name": "newfolder",
        "mimeType": DIR_MIME_TYPE,
    }
    fs.dircache["parent"] = empty_listing()

    result = fs.mkdir("parent/newfolder", create_parents=False)

    mocked_fs.files.create.assert_called_once_with(
        body={
            "name": "newfolder",
            "mimeType": DIR_MIME_TYPE,
            "parents": ["parent-id"],
        },
        supportsAllDrives=True,
    )
    assert result["id"] == "new-id"
    assert fs.dircache["parent/newfolder"] == []
    assert fs.dircache["parent"][0]["name"] == "parent/newfolder"


def test_mkdir_raises_when_path_exists(mocked_fs: MockedDriveFS) -> None:
    mocked_fs.fs.exists = mock.Mock(return_value=True)
    mocked_fs.fs.info = mock.Mock(return_value={"id": "parent-id"})

    with pytest.raises(FileExistsError):
        mocked_fs.fs.mkdir("parent/existing", create_parents=False)


def test_mkdir_create_parents_calls_makedirs(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.makedirs = mock.Mock()
    fs.exists = mock.Mock(return_value=False)
    fs.info = mock.Mock(return_value={"id": "parent-id"})
    mocked_fs.files.create.return_value.execute.return_value = {
        "id": "new-id",
        "name": "child",
        "mimeType": DIR_MIME_TYPE,
    }

    fs.mkdir("parent/child", create_parents=True)

    fs.makedirs.assert_called_once_with("parent", exist_ok=True)


def test_mkdir_skips_parent_creation_at_root(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.makedirs = mock.Mock()
    fs.exists = mock.Mock(return_value=False)
    fs.info = mock.Mock(return_value={"id": "root-id"})
    mocked_fs.files.create.return_value.execute.return_value = {
        "id": "new-id",
        "name": "top",
        "mimeType": DIR_MIME_TYPE,
    }

    fs.mkdir("top", create_parents=True)

    fs.makedirs.assert_not_called()


def test_makedirs_creates_missing_segments(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.exists = mock.Mock(side_effect=[False, False, True])
    fs.mkdir = mock.Mock()

    fs.makedirs("a/b/c", exist_ok=True)

    assert fs.mkdir.call_count == 2
    fs.mkdir.assert_any_call("a", create_parents=False)
    fs.mkdir.assert_any_call("a/b", create_parents=False)


def test_makedirs_exist_ok_false_raises(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.exists = mock.Mock(side_effect=[True, True])

    with pytest.raises(FileExistsError):
        fs.makedirs("a/b", exist_ok=False)


def test_rm_file_deletes_and_updates_dircache(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.info = mock.Mock(return_value={"id": "file-id"})
    fs.dircache["parent"] = [{"name": "parent/file", "id": "file-id"}]
    fs.dircache["parent/file"] = empty_listing()

    fs.rm_file("parent/file")

    mocked_fs.files.delete.assert_called_once_with(
        fileId="file-id", supportsAllDrives=True
    )
    assert fs.dircache["parent"] == []
    assert "parent/file" not in fs.dircache


def test_rm_file_uses_explicit_file_id(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.info = mock.Mock()

    fs.rm_file("parent/file", file_id="explicit-id")

    mocked_fs.files.delete.assert_called_once_with(
        fileId="explicit-id", supportsAllDrives=True
    )
    fs.info.assert_not_called()


def test_rm_non_empty_folder_without_recursive_raises(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    fs.isdir = mock.Mock(return_value=True)
    fs.ls = mock.Mock(return_value=["child"])
    fs.rm_file = mock.Mock()

    with pytest.raises(ValueError, match="non-empty"):
        fs.rm("folder", recursive=False)

    fs.rm_file.assert_not_called()


def test_rm_delegates_to_rm_file(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.isdir = mock.Mock(return_value=False)
    fs.rm_file = mock.Mock()

    fs.rm("file.txt")

    fs.rm_file.assert_called_once_with("file.txt")


def test_rmdir_requires_directory(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.isdir = mock.Mock(return_value=False)

    with pytest.raises(ValueError, match="not a directory"):
        fs.rmdir("file.txt")


def test_rmdir_deletes_empty_directory(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.isdir = mock.Mock(return_value=True)
    fs.ls = mock.Mock(return_value=[])
    fs.rm_file = mock.Mock()

    fs.rmdir("empty")

    fs.rm_file.assert_called_once_with("empty")


def test_ls_from_dircache_returns_sorted_names(
    anon_fs: GoogleDriveFileSystem,
) -> None:
    anon_fs.dircache["parent"] = [
        {"name": "parent/b", "type": "file"},
        {"name": "parent/a", "type": "file"},
    ]

    assert anon_fs.ls("parent") == ["parent/a", "parent/b"]


def test_ls_from_dircache_detail(anon_fs: GoogleDriveFileSystem) -> None:
    listing = [{"name": "parent/a", "type": "file"}]
    anon_fs.dircache["parent"] = listing

    assert anon_fs.ls("parent", detail=True) == listing


def test_ls_nested_directory(anon_fs: GoogleDriveFileSystem) -> None:
    def list_by_id(
        file_id: str,
        trashed: bool = False,
        path_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        if file_id == "root":
            return [
                {
                    "name": "parent",
                    "id": "parent-id",
                    "type": "directory",
                    "mimeType": DIR_MIME_TYPE,
                }
            ]
        if file_id == "parent-id":
            return [
                {
                    "name": "parent/child.txt",
                    "id": "child-id",
                    "type": "file",
                    "mimeType": "text/plain",
                }
            ]
        return []

    anon_fs.root_file_id = "root"
    anon_fs._list_directory_by_id = mock.Mock(side_effect=list_by_id)

    assert anon_fs.ls("parent") == ["parent/child.txt"]
    assert "parent" in anon_fs.dircache
    assert anon_fs.dircache["parent"][0]["name"] == "parent/child.txt"


def test_ls_nested_subpath_uses_parent_info(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs.info = mock.Mock(return_value={"id": "parent-id", "type": "directory"})
    anon_fs._list_directory_by_id = mock.Mock(
        return_value=[
            {
                "name": "parent/child.txt",
                "id": "child-id",
                "type": "file",
                "mimeType": "text/plain",
            }
        ],
    )

    assert anon_fs.ls("parent/child.txt") == ["parent/child.txt"]
    anon_fs.info.assert_called_once_with("parent", trashed=False)


def test_info_non_root_delegates_to_parent(anon_fs: GoogleDriveFileSystem) -> None:
    listing = [
        {
            "name": "file.txt",
            "id": "file-id",
            "type": "file",
            "size": 3,
            "mimeType": "text/plain",
        }
    ]
    anon_fs.dircache[""] = listing

    info = anon_fs.info("file.txt")

    assert info["id"] == "file-id"


def test_open_returns_google_drive_file(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.info = mock.Mock(
        return_value={"id": "file-id", "size": 0, "type": "file", "name": "file.txt"}
    )

    opened = fs._open("file.txt", mode="rb")

    assert isinstance(opened, GoogleDriveFile)
    assert opened.path == "file.txt"


def test_ls_file_at_root_returns_parent_listing(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs._list_directory_by_id = mock.Mock(
        return_value=[
            {
                "name": "file.txt",
                "id": "file-id",
                "type": "file",
                "mimeType": "text/plain",
            }
        ]
    )

    assert anon_fs.ls("file.txt") == ["file.txt"]


def test_ls_raises_multiple_files_error(anon_fs: GoogleDriveFileSystem) -> None:
    anon_fs._list_directory_by_id = mock.Mock(
        return_value=[
            {"name": "dup", "id": "1", "type": "file"},
            {"name": "dup", "id": "2", "type": "file"},
        ]
    )

    with pytest.raises(MultipleFilesError):
        anon_fs.ls("dup")


def test_list_directory_by_id_paginates(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.side_effect = [
        {"files": [{"name": "a", "id": "1"}], "nextPageToken": "page-2"},
        {"files": [{"name": "b", "id": "2"}]},
    ]
    mocked_fs.files.list.return_value = list_request

    result = fs._list_directory_by_id("folder-id")

    assert len(result) == 2
    assert mocked_fs.files.list.call_count == 2
    assert mocked_fs.files.list.call_args_list[1].kwargs["pageToken"] == "page-2"


def test_list_directory_by_id_shared_drive_root_query(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    fs.drive = "drive-123"
    list_request = mock.Mock()
    list_request.execute.return_value = empty_files_list_response()
    mocked_fs.files.list.return_value = list_request

    fs._list_directory_by_id(ROOT_ID)

    query = mocked_fs.files.list.call_args.kwargs["q"]
    assert "'drive-123' in parents" in query
    assert "trashed = false" in query


def test_list_directory_by_id_includes_trashed_when_requested(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.return_value = empty_files_list_response()
    mocked_fs.files.list.return_value = list_request

    fs._list_directory_by_id("folder-id", trashed=True)

    query = mocked_fs.files.list.call_args.kwargs["q"]
    assert "trashed = false" not in query


def test_list_directory_by_id_passes_drive_kwargs(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.drive = "drive-123"
    list_request = mock.Mock()
    list_request.execute.return_value = empty_files_list_response()
    mocked_fs.files.list.return_value = list_request

    fs._list_directory_by_id("folder-id")

    kwargs = mocked_fs.files.list.call_args.kwargs
    assert kwargs["driveId"] == "drive-123"
    assert kwargs["supportsAllDrives"] is True


def test_drives_paginates(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    list_request = mock.Mock()
    list_request.execute.side_effect = [
        {"drives": [{"id": "1", "name": "a"}], "nextPageToken": "t"},
        {"drives": [{"id": "2", "name": "b"}]},
    ]
    mocked_fs.service.drives.return_value.list.return_value = list_request

    assert fs.drives == [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}]


@pytest.mark.xfail(
    reason="export() calls self.path_to_file_id, which does not exist; "
    "it should resolve the id via self.info(path)['id'] like rm_file/ls",
    strict=True,
    raises=AttributeError,
)
def test_export_calls_files_export_when_path_resolves(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    fs.info = mock.Mock(return_value={"id": "doc-id"})
    mocked_fs.files.export.return_value.execute.return_value = b"exported"

    result = fs.export("doc.gdoc", "text/plain")

    mocked_fs.files.export.assert_called_once_with(
        fileId="doc-id", mimeType="text/plain", supportsAllDrives=True
    )
    assert result == b"exported"
