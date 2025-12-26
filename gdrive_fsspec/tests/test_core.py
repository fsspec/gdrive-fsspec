import os

import pytest

import gdrive_fsspec


testdir = "gdrive_fsspec_testdir"
kwargs = {
    "creds": os.getenv("GDRIVE_FSSPEC_CREDENTIALS_PATH"),
    "token": os.getenv("GDRIVE_FSSPEC_CREDENTIALS_TYPE", "service_account"),
    "drive": os.getenv("GDRIVE_FSSPEC_DRIVE"),
}


@pytest.fixture()
def fs():
    fs = gdrive_fsspec.GoogleDriveFileSystem(**kwargs)
    if fs.exists(testdir):
        fs.rm(testdir, recursive=True)
    fs.mkdir(testdir, create_parents=True)
    try:
        yield fs
    finally:
        try:
            fs.rm(testdir, recursive=True)
        except IOError:
            pass


def test_create_anon():
    fs = gdrive_fsspec.GoogleDriveFileSystem(token="anon")
    assert fs.srv is not None


def test_simple(fs):
    assert fs.ls("")
    data = b"hello"
    fn = testdir + "/testfile"
    with fs.open(fn, "wb") as f:
        f.write(data)
    assert fs.cat(fn) == data


def test_create_directory(fs):
    fs.makedirs(testdir + "/data")
    fs.makedirs(testdir + "/data/bar/baz")

    assert fs.exists(testdir + "/data")
    assert fs.exists(testdir + "/data/bar")
    assert fs.exists(testdir + "/data/bar/baz")

    data = b"intermediate path"
    with fs.open(testdir + "/data/bar/test", "wb") as f:
        f.write(data)
    assert fs.cat(testdir + "/data/bar/test") == data
