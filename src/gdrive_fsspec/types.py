"""Static types for Google Drive fsspec integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from googleapiclient._apis.drive.v3.resources import DriveResource
    from googleapiclient._apis.drive.v3.schemas import Drive, File

    FilesResource = DriveResource.FilesResource


class _FsspecRequired(TypedDict):
    """Fields fsspec requires on every file-info dict."""

    name: str
    size: int
    type: Literal["file", "directory"]


class _DrivePartialFields(TypedDict, total=False):
    """Subset of Drive v3 ``File`` fields requested via ``FIELDS``."""

    id: str
    mimeType: str
    trashed: bool
    version: str
    createdTime: str
    modifiedTime: str


class FileInfo(_FsspecRequired, _DrivePartialFields):
    """fsspec file-info dict: normalized path fields plus Drive API metadata."""


__all__ = ["Drive", "DriveResource", "File", "FileInfo", "FilesResource"]
