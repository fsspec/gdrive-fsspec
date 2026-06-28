# ---------------------------------------------------------------------------
# Integration tests (require live Google Drive credentials)
#
# Run: uv run pytest -v -m integration
#
# Auth (pick one):
#   Service account (CI default):
#     GDRIVE_FSSPEC_CREDENTIALS_PATH=/path/to/sa.json
#     GDRIVE_FSSPEC_DRIVE=your-shared-drive-name
#   User OAuth (My Drive or a shared drive you can access):
#     GDRIVE_FSSPEC_CREDENTIALS_TYPE=cache   # or browser for first login
#     GDRIVE_FSSPEC_DRIVE=optional-shared-drive-name
# ---------------------------------------------------------------------------

import pytest
from conftest import TESTDIR, FsFactory

from gdrive_fsspec.core import GoogleDriveFileSystem

# Listing cache can be stale or wrong after writes/deletes.
DIRCACHE_XFAIL = pytest.mark.xfail(
    reason="dircache not updated correctly after mutations",
    strict=True,
)


def _test_path(name: str) -> str:
    return f"{TESTDIR}/{name}"


@pytest.mark.integration
def test_simple(fs: GoogleDriveFileSystem) -> None:
    assert fs.ls("")
    data = b"hello"
    filename = _test_path("testfile")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(data)
    assert fs.cat(filename) == data


@pytest.mark.integration
def test_create_directory(fs: GoogleDriveFileSystem) -> None:
    fs.makedirs(_test_path("data"))
    fs.makedirs(_test_path("data/bar/baz"))

    assert fs.exists(_test_path("data"))
    assert fs.exists(_test_path("data/bar"))
    assert fs.exists(_test_path("data/bar/baz"))

    data = b"intermediate path"
    with fs.open(_test_path("data/bar/test"), "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(data)
    assert fs.cat(_test_path("data/bar/test")) == data


@pytest.mark.integration
def test_rm_file_removes_from_listing(fs: GoogleDriveFileSystem) -> None:
    filename = _test_path("to_delete")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"gone soon")

    assert fs.exists(filename)
    fs.rm(filename)
    assert not fs.exists(filename)


@pytest.mark.integration
@DIRCACHE_XFAIL
def test_rm_recursive_deletes_directory_tree(fs: GoogleDriveFileSystem) -> None:
    root = _test_path("tree")
    fs.makedirs(root + "/a/b")
    with fs.open(root + "/a/b/leaf", "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"leaf")

    fs.rm(root, recursive=True)

    assert not fs.exists(root)
    assert not fs.exists(root + "/a")
    assert not fs.exists(root + "/a/b/leaf")


@pytest.mark.integration
def test_rmdir_empty_directory(fs: GoogleDriveFileSystem) -> None:
    path = _test_path("empty_dir")
    fs.mkdir(path)

    assert fs.exists(path)
    fs.rmdir(path)
    assert not fs.exists(path)


@pytest.mark.integration
def test_rmdir_non_empty_raises(fs: GoogleDriveFileSystem) -> None:
    path = _test_path("nonempty_dir")
    fs.mkdir(path)
    with fs.open(path + "/child", "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"x")

    with pytest.raises(ValueError, match="non-empty"):
        fs.rmdir(path)


@pytest.mark.integration
def test_read_with_seek(fs: GoogleDriveFileSystem) -> None:
    data = b"0123456789abcdef"
    filename = _test_path("seekable")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(data)

    with fs.open(filename, "rb") as f:
        f.seek(4)
        assert f.read(4) == b"4567"
        assert f.read(2) == b"89"


# @pytest.mark.integration
# def test_multiblock_upload(fs: GoogleDriveFileSystem) -> None:
#     """Exercise resumable upload with a small block size (not the 5 MiB default)."""
#     data = b"x" * 5000
#     fn = _test_path("multiblock")
#     with fs.open(fn, "wb", block_size=1024) as f:
#         f.write(data)

#     assert fs.cat(fn) == data


@pytest.mark.integration
@DIRCACHE_XFAIL
def test_ls_detail_includes_metadata(fs: GoogleDriveFileSystem) -> None:
    filename = _test_path("detail_check")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"meta")

    entries = fs.ls(TESTDIR, detail=True)
    match = [e for e in entries if e["name"] == filename]
    assert len(match) == 1
    assert match[0]["type"] == "file"
    assert match[0]["size"] == 4
    assert "id" in match[0]


@pytest.mark.integration
@DIRCACHE_XFAIL
def test_nested_ls_lists_children(fs: GoogleDriveFileSystem) -> None:
    parent = _test_path("nested_parent")
    fs.mkdir(parent)
    with fs.open(parent + "/child.txt", "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"child")

    names = fs.ls(parent)
    assert parent + "/child.txt" in names


@pytest.mark.integration
def test_root_file_id_rejects_file(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """A regular file ID must not be accepted as the filesystem root."""
    filename = _test_path("root_is_a_file")
    with fs.open(filename, "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"x")
    file_id = fs.info(filename)["id"]

    with pytest.raises(NotADirectoryError):
        make_fs(root_file_id=file_id)


@pytest.mark.integration
def test_root_file_id_accepts_folder(
    fs: GoogleDriveFileSystem, make_fs: FsFactory
) -> None:
    """A folder ID is a valid root and lists its children from ``ls("")``."""
    folder = _test_path("root_folder")
    fs.mkdir(folder)
    with fs.open(folder + "/child", "wb") as f:
        # pyrefly: ignore [bad-argument-type]
        f.write(b"data")
    folder_id = fs.info(folder)["id"]

    rooted = make_fs(root_file_id=folder_id)
    names = [item["name"] for item in rooted.ls("", detail=True)]
    assert "child" in names


@pytest.mark.integration
def test_root_file_id_scoped_to_shared_drive(
    fs: GoogleDriveFileSystem,
    make_fs: FsFactory,
    requires_shared_drive: None,
) -> None:
    """A folder inside the configured shared drive works as ``root_file_id``."""
    folder = _test_path("scoped_root")
    fs.mkdir(folder)
    folder_id = fs.info(folder)["id"]

    rooted = make_fs(root_file_id=folder_id)
    assert rooted.root_file_id == folder_id
    assert rooted.ls("") == []


@pytest.mark.integration
def test_shared_drive_root_lists(
    make_fs: FsFactory,
    requires_shared_drive: None,
) -> None:
    """Listing ``ls("")`` at the shared-drive root succeeds."""
    drive_fs = make_fs()
    listing = drive_fs.ls("")
    assert isinstance(listing, list)


@pytest.mark.integration
def test_info_returns_directory_for_root(fs: GoogleDriveFileSystem) -> None:
    info = fs.info("")
    assert info["type"] == "directory"
    assert info["size"] == 0
    assert info["id"] == fs.root_file_id
