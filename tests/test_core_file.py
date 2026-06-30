"""Unit tests for GoogleDriveFile read/write behavior."""

import io
import json
from typing import Any
from unittest import mock

import pytest
from conftest import MockedDriveFS, empty_headers, empty_listing
from googleapiclient.errors import HttpError

from gdrive_fsspec.core import (
    GoogleDriveFile,
    GoogleDriveFileSystem,
    MultipleFilesError,
)


def _http_error(message: str) -> HttpError:
    response = mock.Mock(status=416, reason="Range Not Satisfiable")
    return HttpError(response, message.encode())


def _write_file(
    fs: GoogleDriveFileSystem,
    path: str = "parent/file.txt",
    existing_id: str | None = None,
    parent_id: str = "parent-id",
) -> GoogleDriveFile:
    """Build a ``wb`` file, stubbing ``fs.info`` for the path and its parent.

    ``existing_id`` is the id returned for ``path`` itself (``None`` means the
    path does not exist yet, so the upload should create a new file).
    """
    parent = fs._parent(path)

    def _info(p: str, *args: object, **kwargs: object) -> dict[str, Any]:
        if fs._strip_protocol(p) == parent:
            return {"id": parent_id, "type": "directory"}
        if existing_id is not None:
            return {"id": existing_id, "type": "file"}
        raise FileNotFoundError(p)

    fs.info = mock.Mock(side_effect=_info)
    file = GoogleDriveFile(fs, path, mode="wb")
    file.location = "https://example.invalid/upload?upload_id=abc"
    return file


def _read_file(
    fs: GoogleDriveFileSystem, path: str = "path/file.txt"
) -> GoogleDriveFile:
    fs.info = mock.Mock(
        return_value={"id": "file-id", "size": 5, "type": "file", "name": path}
    )
    return GoogleDriveFile(fs, path, mode="rb")


def test_fetch_range_full_read(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    media = mock.Mock()
    media.headers = empty_headers()
    media.execute.return_value = b"hello"
    mocked_fs.files.get_media.return_value = media

    file = _read_file(fs)
    data = file._fetch_range()

    assert data == b"hello"
    mocked_fs.files.get_media.assert_called_once_with(
        fileId="file-id", supportsAllDrives=True
    )
    assert "Range" not in media.headers


def test_fetch_range_with_byte_range(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    media = mock.Mock()
    media.headers = empty_headers()
    media.execute.return_value = b"hel"  # codespell:ignore hel
    mocked_fs.files.get_media.return_value = media

    file = _read_file(fs)
    file._fetch_range(0, 3)

    assert media.headers["Range"] == "bytes=0-2"


def test_fetch_range_not_satisfiable_returns_empty(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    media = mock.Mock()
    media.headers = empty_headers()
    media.execute.side_effect = _http_error("not satisfiable")
    mocked_fs.files.get_media.return_value = media

    file = _read_file(fs)

    assert file._fetch_range(100, 200) == b""


def test_fetch_range_propagates_other_http_errors(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    media = mock.Mock()
    media.headers = empty_headers()
    media.execute.side_effect = HttpError(
        mock.Mock(status=403, reason="Forbidden"), b"denied"
    )
    mocked_fs.files.get_media.return_value = media

    file = _read_file(fs)

    with pytest.raises(HttpError):
        file._fetch_range()


def test_initiate_upload_new_file(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = (
        {"status": "200", "location": "https://upload.example/resume?upload_id=xyz"},
        b"",
    )

    file = _write_file(fs)
    assert file.file_id is None
    try:
        file._initiate_upload()
    finally:
        file.closed = True

    assert file.location == "https://upload.example/resume?upload_id=xyz"
    args, kwargs = mocked_fs.files._http.request.call_args
    assert kwargs["method"] == "POST"
    assert args[0].endswith("/files?uploadType=resumable&supportsAllDrives=true")
    assert json.loads(kwargs["body"].decode()) == {
        "name": "file.txt",
        "parents": ["parent-id"],
    }


def test_initiate_upload_existing_file_patches(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = (
        {"status": "200", "location": "https://upload.example/resume?upload_id=xyz"},
        b"",
    )

    file = _write_file(fs, existing_id="existing-id")
    assert file.file_id == "existing-id"
    try:
        file._initiate_upload()
    finally:
        file.closed = True

    assert file.location == "https://upload.example/resume?upload_id=xyz"
    args, kwargs = mocked_fs.files._http.request.call_args
    assert kwargs["method"] == "PATCH"
    assert args[0].startswith(
        "https://www.googleapis.com/upload/drive/v3/files/existing-id?"
    )
    # No new resource is created, so name/parents are not re-sent.
    assert json.loads(kwargs["body"].decode()) == {}


def test_open_wb_propagates_multiple_files_error(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    # A path that already resolves to duplicates must not be overwritten by
    # creating a third copy; surface the ambiguity instead.
    fs.info = mock.Mock(side_effect=MultipleFilesError("parent/file.txt"))

    with pytest.raises(MultipleFilesError):
        GoogleDriveFile(fs, "parent/file.txt", mode="wb")


def test_open_wb_on_directory_raises(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    # Opening an existing directory for writing must not resolve its id as an
    # overwrite target; bytes cannot be PATCHed onto a folder.
    fs.info = mock.Mock(
        return_value={"id": "dir-id", "type": "directory", "name": "parent/sub"}
    )

    with pytest.raises(IsADirectoryError):
        GoogleDriveFile(fs, "parent/sub", mode="wb")


def test_upload_chunk_partial(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = (
        {"status": "308", "range": "0-999"},
        b"",
    )
    file = _write_file(fs)
    file.write(b"x" * 1000)
    file.offset = 0

    try:
        file._upload_chunk(final=False)
    finally:
        file.closed = True

    headers = mocked_fs.files._http.request.call_args.kwargs["headers"]
    assert headers["Content-Range"] == "bytes 0-999/*"


def test_upload_chunk_final_updates_dircache(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    fs.dircache["parent"] = empty_listing()
    mocked_fs.files._http.request.return_value = (
        {"status": "200"},
        json.dumps(
            {"id": "file-id", "name": "file.txt", "mimeType": "text/plain"}
        ).encode(),
    )
    file = _write_file(fs)
    file.write(b"data")
    file.offset = 0

    try:
        file._upload_chunk(final=True)
    finally:
        file.closed = True

    assert file.file_id == "file-id"
    assert fs.dircache["parent"] == [
        {
            "id": "file-id",
            "name": "parent/file.txt",
            "mimeType": "text/plain",
            "size": 4,
            "type": "file",
        }
    ]


def test_upload_chunk_final_overwrites_dircache_entry(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    # Existing listing already has the file; overwriting must not duplicate it.
    fs.dircache["parent"] = [
        {"name": "parent/file.txt", "id": "old-id", "size": 1, "type": "file"}
    ]
    mocked_fs.files._http.request.return_value = (
        {"status": "200"},
        json.dumps(
            {"id": "new-id", "name": "file.txt", "mimeType": "text/plain"}
        ).encode(),
    )
    file = _write_file(fs, existing_id="old-id")
    file.write(b"hello")
    file.offset = 0

    try:
        file._upload_chunk(final=True)
    finally:
        file.closed = True

    # Whole-listing compare: the single entry is fully replaced, with no stale
    # fields (old id/size) leaking through from the pre-existing entry.
    assert fs.dircache["parent"] == [
        {
            "id": "new-id",
            "name": "parent/file.txt",
            "mimeType": "text/plain",
            "size": 5,
            "type": "file",
        }
    ]
    assert file.file_id == "new-id"


def test_upload_chunk_final_empty_buffer(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = (
        {"status": "200"},
        json.dumps({"id": "file-id", "name": "file.txt"}).encode(),
    )
    file = _write_file(fs)
    file.buffer = io.BytesIO()
    file.offset = 10
    file.autocommit = True

    try:
        file._upload_chunk(final=True)
    finally:
        file.closed = True

    headers = mocked_fs.files._http.request.call_args.kwargs["headers"]
    assert headers["Content-Range"] == "bytes */10"
    assert mocked_fs.files._http.request.call_args.kwargs["body"] is None


def test_upload_chunk_unexpected_status_raises(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = ({"status": "500"}, b"error")
    file = _write_file(fs)
    file.write(b"data")
    file.offset = 0

    with pytest.raises(AssertionError):
        try:
            file._upload_chunk(final=False)
        finally:
            file.closed = True


def test_upload_chunk_unexpected_response_raises_ioerror(
    mocked_fs: MockedDriveFS,
) -> None:
    fs = mocked_fs.fs
    mocked_fs.files._http.request.return_value = ({"status": "204"}, b"")
    file = _write_file(fs)
    file.write(b"data")
    file.offset = 0

    with pytest.raises(IOError):
        try:
            file._upload_chunk(final=False)
        finally:
            file.closed = True


def test_commit_finalizes_upload(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    file = _write_file(fs)
    file.autocommit = False

    with mock.patch.object(file, "_upload_chunk") as upload:
        try:
            file.commit()
        finally:
            file.closed = True

    assert file.autocommit
    upload.assert_called_once_with(final=True)


def test_discard_noop_without_location(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    call_mock = mock.Mock()
    fs._call = call_mock  # pyrefly: ignore [missing-attribute]
    file = _write_file(fs)
    file.location = None

    try:
        file.discard()
    finally:
        file.closed = True

    call_mock.assert_not_called()


def test_discard_cancels_resumable_upload(mocked_fs: MockedDriveFS) -> None:
    fs = mocked_fs.fs
    call_mock = mock.Mock(return_value=({"status": "204"}, b""))
    fs._call = call_mock  # pyrefly: ignore [missing-attribute]
    file = _write_file(fs)
    file.location = "https://upload.example/resume?upload_id=abc123"

    try:
        file.discard()
    finally:
        file.closed = True

    call_mock.assert_called_once_with(
        "DELETE",
        "https://www.googleapis.com/upload/drive/v3/files",
        params={"uploadType": "resumable", "upload_id": ["abc123"]},
    )
