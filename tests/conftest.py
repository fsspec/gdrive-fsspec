import pytest

from gdrive_fsspec.core import GoogleDriveFileSystem


@pytest.fixture()
def anon_fs():
    # skip_instance_cache keeps each test's dircache isolated; fsspec otherwise
    # returns the same cached instance for identical constructor arguments.
    return GoogleDriveFileSystem(token="anon", skip_instance_cache=True)
