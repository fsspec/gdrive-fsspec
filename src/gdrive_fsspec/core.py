from __future__ import annotations

import io
import json
import logging
import os
import pathlib
import re
import warnings
from functools import cached_property
from typing import TYPE_CHECKING, Any, Literal, Mapping, TypeAlias, cast, overload

from fsspec.spec import AbstractBufferedFile, AbstractFileSystem
from google.auth.credentials import AnonymousCredentials, Credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from .types import FileInfo
from .typing_utils import override

if TYPE_CHECKING:
    from googleapiclient._apis.drive.v3.resources import DriveResource
    from googleapiclient._apis.drive.v3.schemas import Drive, File

    from .types import FilesResource

DEFAULT_BLOCK_SIZE = 5 * 2**20
LOGGER = logging.getLogger("gdrive_fsspec")

# https://developers.google.com/workspace/drive/api/guides/api-specific-auth
SCOPE_DICT = {
    "full_control": "https://www.googleapis.com/auth/drive",
    "read_only": "https://www.googleapis.com/auth/drive.readonly",
}

# https://developers.google.com/workspace/drive/api/guides/mime-types
DIR_MIME_TYPE = "application/vnd.google-apps.folder"

# File resource fields; partial-response mask for files.list / files.get:
# https://developers.google.com/workspace/drive/api/reference/rest/v3/files#resource
# https://developers.google.com/workspace/drive/api/guides/performance#partial
FIELDS = ",".join(
    [
        "name",
        "id",
        "size",
        "trashed",
        "mimeType",
        "version",
        "createdTime",
        "modifiedTime",
    ]
)


def _normalize_path(prefix: str, name: str) -> str:
    raw_prefix = prefix.strip("/")
    return "/" + "/".join([raw_prefix, name])


def _finfo_from_response(
    f: File | Mapping[str, Any], path_prefix: str | None = None
) -> FileInfo:
    # strictly speaking, other types might be capable of having children,
    # such as packages
    # TODO: check specifically for links
    ftype = "directory" if f.get("mimeType") == DIR_MIME_TYPE else "file"
    if path_prefix:
        name = _normalize_path(path_prefix, f["name"])
    else:
        name = f["name"]
    info: FileInfo = {
        "name": name.lstrip("/"),
        "size": int(f.get("size", 0)),
        "type": ftype,
    }
    return cast(FileInfo, {**f, **info})


class MultipleFilesError(FileNotFoundError):
    pass


AuthMethod = Literal["anon", "browser", "cache", "service_account"]
ROOT_ID = "root"

# One path element — matches fsspec.stringify_path
PathLike: TypeAlias = str | os.PathLike[str] | pathlib.Path


class GoogleDriveFileSystem(AbstractFileSystem):
    """Access Google Drive as a file system.

    In the Google Drive API, everything is a file resource. Folders are files
    with a special MIME type.

    Note:
        We assume that each path identifies a unique file. In Google Drive, it
        is possible to have multiple identically named files, and this will
        result in errors in this implementation.
    """

    protocol = "gdrive"
    root_marker = ""

    if TYPE_CHECKING:
        service: DriveResource
        files: FilesResource

    def __init__(
        self,
        root_file_id: str | None = None,
        token: AuthMethod = "cache",
        access: Literal["full_control", "read_only"] = "full_control",
        spaces: str = "drive",
        creds: dict[str, Any] | str | None = None,
        drive: str | None = None,
        auth_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize a Google Drive file system.

        Args:
            root_file_id: Folder file ID to use as the filesystem root (the empty
                path ``""``). Obtain it from a folder URL such as
                ``https://drive.google.com/drive/folders/<id>``. If omitted,
                defaults to the shared-drive root when ``drive`` is set, otherwise
                ``"root"`` (the authenticated user's My Drive). A shared-drive ID
                is also accepted here for backwards compatibility (when it cannot
                be resolved as a file, it is treated as a shared drive and
                ``drive`` is set from it), but this is a legacy path; prefer
                passing ``drive`` to target a shared drive.
            token: One of ``"anon"``, ``"browser"``, ``"cache"``,
                ``"service_account"``. Using ``"browser"`` will prompt a URL to
                be opened in a browser and cache the response for future use with
                ``token="cache"``. ``"browser"`` removes any previously cached
                token file, if it exists.
            access: One of ``"full_control"``, ``"read_only"``.
            spaces: Category of files to search; can be ``"drive"``,
                ``"appDataFolder"``, and ``"photos"``. Of these, only the first
                is general.
            creds: Required for ``"service_account"`` token. A dict with the
                service account credentials from the GCP console (same content
                as the downloaded JSON). See
                https://cloud.google.com/iam/docs/service-account-creds#key-types.
                Files must be shared with the service account email from that JSON.
            drive: A shared drive to scope API calls to, given as either its ID
                or its name. Resolved against the drives accessible to the current
                credentials (via ``drives.list``). Required for service-account
                uploads. If omitted, operations use the user's My Drive (or
                anonymous public files when ``token="anon"``). Combine with
                ``root_file_id`` to start below the shared-drive root, e.g. a
                subfolder ID inside that drive.
            auth_kwargs: Additional keyword arguments passed to the authentication
                backend (``pydata_google_auth.get_user_credentials`` for user
                OAuth, or
                ``service_account.Credentials.from_service_account_info`` for
                service accounts). For headless or remote environments where a
                local callback server is unavailable, pass
                ``use_local_webserver=False`` to request a token via the console.
            **kwargs: Passed to the parent class.
        """
        # Ideally, these should be keyword-arguments, but to maintain backwards compatibility, we keep the existing API.

        super().__init__(**kwargs)
        self.access = access
        self.scopes = [SCOPE_DICT[access]]
        self.spaces = spaces
        self.creds = creds
        self.drive = drive
        self.auth_kwargs = auth_kwargs or {}
        self.connect(method=token)
        if token == "anon":
            self.drive = None
        elif drive:
            self.drive = self._resolve_drive_id(drive)

        if root_file_id and root_file_id != ROOT_ID:
            self._validate_root_file_id(root_file_id)

        self.root_file_id = root_file_id or self.drive or ROOT_ID

    def _validate_root_file_id(self, root_file_id: str) -> None:
        try:
            meta = self.files.get(
                fileId=root_file_id,
                fields="id,trashed,mimeType,driveId",
                supportsAllDrives=True,
            ).execute()
        except HttpError as err:
            if err.status_code != 404:
                raise
            self._confirm_shared_drive_root(root_file_id)
            return

        if meta.get("trashed"):
            raise FileNotFoundError(f"root_file_id {root_file_id!r} is trashed")
        if meta.get("mimeType") != DIR_MIME_TYPE:
            raise NotADirectoryError(f"root_file_id {root_file_id!r} is not a folder")
        # Check if the root file is in the correct drive.
        if self.drive is not None and meta.get("driveId") != self.drive:
            raise ValueError(
                f"root_file_id {root_file_id!r} is not in drive {self.drive!r}"
            )

    def _confirm_shared_drive_root(self, drive_id: str) -> None:
        """Accept a shared-drive ID passed as ``root_file_id`` (legacy).

        Older versions documented ``root_file_id`` as accepting a "share, drive or
        folder ID", so a shared-drive ID may be passed here. When the ID does not
        resolve as a file, fall back to treating it as a shared drive and set
        ``self.drive`` from it so directory listings are scoped correctly.

        Args:
            drive_id: Shared-drive ID to validate.

        Note:
            Prefer passing ``drive`` instead of using this legacy path.
        """
        try:
            self.service.drives().get(driveId=drive_id).execute()
        except HttpError as err:
            if err.status_code == 404:
                raise FileNotFoundError(f"root_file_id {drive_id!r} not found") from err
            raise
        if self.drive is None:
            self.drive = drive_id
        elif self.drive != drive_id:
            raise ValueError(
                f"root_file_id {drive_id!r} conflicts with drive {self.drive!r}"
            )

    def connect(self, method: AuthMethod | None = None) -> None:
        if method == "browser":
            cred = self._connect_browser()
        elif method == "cache":
            cred = self._connect_cache()
        elif method == "anon":
            cred = AnonymousCredentials()
        elif method == "service_account":
            cred = self._connect_service_account()
        else:
            raise ValueError(f"Invalid connection method `{method}`.")
        self.service = build("drive", "v3", credentials=cred)
        self.files = self.service.files()

    @property
    def srv(self) -> DriveResource:
        """Deprecated alias for :attr:`service`."""
        warnings.warn(
            "`srv` is deprecated; use `service` instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.service

    @property
    def _user_credentials_cache_path(self) -> str:
        import pydata_google_auth.cache

        return pydata_google_auth.cache.READ_WRITE._path

    def _connect_browser(self) -> Credentials:
        try:
            os.remove(self._user_credentials_cache_path)
        except OSError:
            pass
        return self._connect_cache()

    def _connect_cache(self) -> Credentials:
        import pydata_google_auth

        kwargs = {"use_local_webserver": True, **self.auth_kwargs}
        return pydata_google_auth.get_user_credentials(self.scopes, **kwargs)

    def _connect_service_account(self) -> Credentials:
        if isinstance(self.creds, str):
            if not self.creds.strip():
                raise ValueError("Empty credentials are not allowed")
            if self.creds[0] != "{":
                creds = json.load(open(self.creds))
            else:
                creds = json.loads(self.creds)
        else:
            creds = self.creds
        return service_account.Credentials.from_service_account_info(
            info=creds, scopes=self.scopes, **self.auth_kwargs
        )

    @cached_property
    def drives(self) -> list[Drive]:
        """Shared drives accessible to the current user.

        Returns:
            List of drive resource dicts from the Drive API.
        """
        drives: list[Drive] = []
        page_token: str | None = None
        while True:
            if page_token is None:
                response = self.service.drives().list().execute()
            else:
                response = self.service.drives().list(pageToken=page_token).execute()
            drives.extend(response["drives"])
            page_token = response.get("nextPageToken")
            if page_token is None:
                break
        return drives

    @cached_property
    def export_formats(self) -> dict[str, list[str]]:
        """Supported export conversions, keyed by source MIME type.

        Maps each Google-native MIME type (e.g.
        ``application/vnd.google-apps.document``) to the list of target MIME
        types it can be exported to. Fetched once from the Drive ``about``
        resource and cached for the lifetime of the filesystem.

        Returns:
            Mapping of source MIME type to its list of valid export targets.
        """
        about = self.service.about().get(fields="exportFormats").execute()
        return about.get("exportFormats", {})

    def _path_str(self, path: PathLike) -> str:
        """Strip the protocol and normalize a path-like input to a single string.

        Args:
            path: A ``str``, ``os.PathLike``, or ``pathlib.Path``.

        Returns:
            The protocol-stripped path as a string.

        Raises:
            TypeError: If ``path`` resolves to a sequence of paths.
        """
        stripped = self._strip_protocol(path)
        if isinstance(stripped, list):
            raise TypeError("expected a single path, not a sequence of paths")
        return stripped

    # Return type is Any (not drive.v3 File) to match fsspec AbstractFileSystem.mkdir.
    @override
    def mkdir(self, path: PathLike, create_parents: bool = True, **kwargs: Any) -> Any:
        """Create a directory at the given path.

        Args:
            path: Directory path to create.
            create_parents: If True, create any missing parent directories first.
            **kwargs: Ignored; accepted for fsspec compatibility.

        Returns:
            The created folder's file resource dict from the Drive API (drive.v3
            ``File`` at runtime).

        Raises:
            FileExistsError: If a file or folder already exists at ``path``.
        """
        if create_parents and self._parent(path):
            self.makedirs(self._parent(path), exist_ok=True)
        par = self._parent(path)
        parent_id = self.info(par)["id"]
        stripped_path = self._path_str(path)
        meta = {
            "name": stripped_path.rstrip("/").rsplit("/", 1)[-1],
            "mimeType": DIR_MIME_TYPE,
            "parents": [parent_id],
        }
        if self.exists(stripped_path):
            raise FileExistsError(stripped_path)
        LOGGER.debug(f"Creating {stripped_path}, child of {parent_id}")
        out: File = self.files.create(body=meta, supportsAllDrives=True).execute()
        if par in self.dircache:
            self.dircache[par].append(_finfo_from_response(out, path_prefix=par))
        self.dircache[stripped_path] = []
        return out

    @override
    def makedirs(self, path: PathLike, exist_ok: bool = True) -> None:
        """Create a directory and any missing parent directories.

        Args:
            path: Directory path to create (may include nested components).
            exist_ok: If False, raise when the final path component already exists.

        Raises:
            FileExistsError: If ``exist_ok`` is False and the directory already exists.
        """
        parts = self._path_str(path).split("/")
        path = ""
        for i, part in enumerate(parts):
            path = path + "/" + part if path else part
            if not self.exists(path):
                self.mkdir(path, create_parents=False)
            elif i == len(parts) - 1 and not exist_ok:
                raise FileExistsError(path)

    @override
    def _rm(self, path: PathLike, file_id: str | None = None) -> None:
        """Delete a single file or directory by path.

        Args:
            path: Path of the file or folder to delete.
            file_id: Optional Drive file ID; if omitted, resolved from ``path``.
        """
        stripped_path = self._path_str(path)
        file_id = file_id or self.info(stripped_path)["id"]
        LOGGER.debug(f"Removing {stripped_path}, file_id={file_id}")
        self.files.delete(fileId=file_id, supportsAllDrives=True).execute()
        parent = self._parent(stripped_path)
        if parent in self.dircache:
            listing = self.dircache[parent]
            i = [i for i, li in enumerate(listing) if li["name"] == stripped_path][0]
            listing.pop(i)
        self.dircache.pop(stripped_path, None)

    @override
    def rm(
        self, path: PathLike, recursive: bool = True, maxdepth: int | None = None
    ) -> None:
        """Delete a file or directory.

        Args:
            path: Path of the file or folder to delete.
            recursive: If False, refuse to delete a non-empty directory.
            maxdepth: Ignored; accepted for fsspec compatibility.

        Raises:
            ValueError: If ``recursive`` is False and the directory is not empty.
        """
        if recursive is False and self.isdir(path) and self.ls(path):
            raise ValueError("Attempt to delete non-empty folder")
        self.rm_file(path)

    @override
    def rmdir(self, path: PathLike) -> None:
        """Remove an empty directory.

        Args:
            path: Path of the directory to remove.

        Raises:
            ValueError: If ``path`` is not a directory or is not empty.
        """
        if not self.isdir(path):
            raise ValueError("Path is not a directory")
        self.rm(path, recursive=False)

    @override
    def invalidate_cache(self, path: PathLike | None = None) -> None:
        if path is None:
            self.dircache.clear()
        else:
            self.dircache.pop(self._strip_protocol(path), None)
        super().invalidate_cache(path)

    def export(self, path: PathLike, mime_type: str) -> bytes:
        """Export a Google-native file to another format and download it.

        Use this for Docs, Sheets, Slides, and other Google Workspace files that
        cannot be downloaded directly with ``open(..., "rb")``.

        Args:
            path: Path of the Google-native file to export.
            mime_type: Target MIME type for the export (e.g. ``"text/plain"``).
                Must be one of the conversions Drive supports for this file's
                type; see :attr:`export_formats`.

        Returns:
            Exported file content as bytes.

        Raises:
            ValueError: If ``mime_type`` is not a supported export target for
                this file's source type.
        """
        info = self.info(path)
        file_id = info["id"]
        source_mime = info.get("mimeType", "")
        targets = self.export_formats.get(source_mime, [])
        if mime_type not in targets:
            valid = ", ".join(targets) if targets else "none"
            raise ValueError(
                f"Cannot export {path!r} (type {source_mime!r}) to "
                f"{mime_type!r}. Supported export types: {valid}."
            )

        request = self.files.export_media(fileId=file_id, mimeType=mime_type)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    def _resolve_drive_id(self, drive: str) -> str:
        """Resolve a shared-drive ID or name to its drive ID.

        Args:
            drive: Shared-drive ID or human-readable name.

        Returns:
            The matching shared-drive ID.

        Raises:
            ValueError: If no drive matches, or the name matches multiple drives.
        """
        if any(d["id"] == drive for d in self.drives):
            return drive
        matches = [d["id"] for d in self.drives if d["name"] == drive]
        if len(matches) == 1:
            return matches[0]
        if len(matches) == 0:
            raise ValueError(f"Drive {drive!r} not found by id or name")
        raise ValueError(f"Drive name {drive!r} refers to multiple shared drives")

    @overload
    # pyrefly: ignore [bad-override]  # overloads diverge from base ls signature
    def ls(
        self,
        path: PathLike,
        detail: Literal[False] = False,
        trashed: bool = False,
        **kwargs: Any,
    ) -> list[str]: ...

    @overload
    def ls(
        self,
        path: PathLike,
        detail: Literal[True],
        trashed: bool = False,
        **kwargs: Any,
    ) -> list[FileInfo]: ...

    @override
    def ls(
        self,
        path: PathLike,
        detail: bool = False,
        trashed: bool = False,
        **kwargs: Any,
    ) -> list[str] | list[FileInfo]:
        """List files and directories under ``path``.

        Args:
            path: Directory path to list. Use ``""`` for the filesystem root.
            detail: If True, return full file-info dicts; otherwise return paths only.
            trashed: If True, include trashed items in the listing.
            kwargs: Ignored; accepted for fsspec compatibility.

        Returns:
            Sorted list of child paths, or list of file-info dicts when ``detail``
            is True.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
            MultipleFilesError: If multiple files share the same path name.
        """
        stripped_path: str = self._path_str(path)
        files: list[FileInfo] | None = self._ls_from_cache(stripped_path)

        if files is None:
            # get parent ID
            if "/" in stripped_path:
                pref = stripped_path.rsplit("/", 1)[0]
                info = self.info(pref, trashed=trashed)
                file_id = info["id"]
            else:
                pref = ""
                file_id = self.root_file_id

            # list parent
            files = self._list_directory_by_id(
                file_id, trashed=trashed, path_prefix=pref
            )
            # An empty listing for the root is a valid, empty directory; for any
            # other path an empty listing means the path does not exist.
            if files or stripped_path == "":
                self.dircache[pref] = files
            else:
                raise FileNotFoundError(stripped_path)

            if stripped_path:
                # else we listed the top-level and are done
                this_file = [f for f in files if f["name"] == stripped_path]
                if len(this_file) == 0:
                    raise FileNotFoundError(stripped_path)
                elif len(this_file) > 1:
                    raise MultipleFilesError(stripped_path)
                if this_file[0]["type"] == "directory":
                    files = self._list_directory_by_id(
                        this_file[0]["id"],
                        trashed=trashed,
                        path_prefix=stripped_path,
                    )
                    self.dircache[stripped_path] = files

        if detail:
            return files
        else:
            return sorted([f["name"] for f in files])

    # Return type is dict[str, Any] (not FileInfo) to match fsspec AbstractFileSystem.info.
    @override
    def info(
        self, path: PathLike, trashed: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        """Return metadata for a file or directory.

        Args:
            path: Path to inspect. Use ``""`` for the filesystem root.
            trashed: If True, allow resolving trashed files.
            kwargs: Ignored; accepted for fsspec compatibility.

        Returns:
            File-info dict including ``name``, ``type``, ``size``, and Drive API
            fields. Shape matches :class:`~gdrive_fsspec.types.FileInfo`.
        """
        stripped_path = self._path_str(path)
        if stripped_path == "":
            info: FileInfo = {
                "name": stripped_path,
                "mimeType": DIR_MIME_TYPE,
                "type": "directory",
                "size": 0,
                "id": self.root_file_id,
            }
            return cast(dict[str, Any], info)
        return super().info(stripped_path, trashed=trashed)

    def _drive_kw(self) -> dict[str, Any]:
        if self.drive is not None:
            return dict(
                includeItemsFromAllDrives=True,
                corpora="drive",
                supportsAllDrives=True,
                driveId=self.drive,
            )
        else:
            empty: dict[str, Any] = {}
            return empty

    def _list_directory_by_id(
        self, file_id: str, trashed: bool = False, path_prefix: str | None = None
    ) -> list[FileInfo]:
        all_files: list[FileInfo] = []
        page_token: str | None = None
        afields = "nextPageToken, files(%s)" % FIELDS
        if file_id == ROOT_ID and self.drive is not None:
            query = f"'{self.drive}' in parents "
        else:
            query = f"'{file_id}' in parents "
        if not trashed:
            query += "and trashed = false "
        kwargs = self._drive_kw()
        while True:
            LOGGER.debug("%s ; prefix %s", query, path_prefix)
            if page_token is None:
                response = self.files.list(
                    q=query,
                    spaces=self.spaces,
                    fields=afields,
                    orderBy="name",
                    pageSize=1000,
                    **kwargs,
                ).execute()
            else:
                response = self.files.list(
                    q=query,
                    spaces=self.spaces,
                    fields=afields,
                    pageToken=page_token,
                    orderBy="name",
                    pageSize=1000,
                    **kwargs,
                ).execute()
            for f in response.get("files", []):
                all_files.append(_finfo_from_response(f, path_prefix))
            page_token = response.get("nextPageToken", None)
            if page_token is None:
                break
        return all_files

    @override
    def _open(
        self,
        path: PathLike,
        mode: str = "rb",
        block_size: int | None = None,
        autocommit: bool = True,
        cache_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AbstractBufferedFile:
        """Open a file on Google Drive, returning a buffered file object.

        Args:
            path: Path of the file to open.
            mode: File mode; only ``"rb"`` and ``"wb"`` are supported.
            block_size: Buffer size in bytes; defaults to ``DEFAULT_BLOCK_SIZE``.
            autocommit: If True, commit the upload when the file is closed.
            cache_options: Options forwarded to the read-ahead cache.
            **kwargs: Passed to :class:`GoogleDriveFile`.

        Returns:
            A :class:`GoogleDriveFile` open in the requested mode.
        """
        return GoogleDriveFile(
            self,
            path,
            mode=mode,
            block_size=block_size if block_size is not None else DEFAULT_BLOCK_SIZE,
            autocommit=autocommit,
            cache_options=cache_options,
            **kwargs,
        )


class GoogleDriveFile(AbstractBufferedFile):
    def __init__(
        self,
        fs: GoogleDriveFileSystem,
        path: PathLike,
        mode: str = "rb",
        block_size: int = DEFAULT_BLOCK_SIZE,
        autocommit: bool = True,
        **kwargs: Any,
    ) -> None:
        """Open a file on Google Drive for reading or writing.

        Args:
            fs: GoogleDriveFileSystem instance.
            path: File path to open.
            mode: File mode; currently only ``"rb"`` and ``"wb"`` are supported.
            block_size: Buffer size for reading or writing (default 5 MiB).
            autocommit: If True, commit the upload when the file is closed.
            **kwargs: Passed to :class:`AbstractBufferedFile`.
        """
        path = fs._path_str(path)
        super().__init__(fs, path, mode, block_size, autocommit=autocommit, **kwargs)

        if mode == "wb":
            self.location = None
        else:
            self.file_id = fs.info(path)["id"]
            self._media_object: Any | None = None

    @override
    def _fetch_range(self, start: int | None = None, end: int | None = None) -> bytes:
        """Fetch bytes from Google Drive for the open file.

        Args:
            start: Start byte offset, or None to fetch from the beginning.
            end: End byte offset (exclusive), or None to fetch through the end.

        Returns:
            Requested byte range, or empty bytes if the range is not satisfiable.
        """

        if self._media_object is None:
            self._media_object = self.fs.files.get_media(
                fileId=self.file_id, supportsAllDrives=True
            )
        if start is not None or end is not None:
            start = start or 0
            end = end or 0
            self._media_object.headers["Range"] = "bytes=%i-%i" % (start, end - 1)
        else:
            self._media_object.headers.pop("Range", None)
        try:
            data = self._media_object.execute()
            return data
        except HttpError as e:
            # TODO : doc says server might send everything if range is outside
            if "not satisfiable" in str(e):
                return b""
            raise

    @override
    def _upload_chunk(self, final: bool = False) -> None:
        """Upload one chunk of a resumable multi-part upload.

        Args:
            final: If True, finalize and commit the upload.

        Raises:
            IOError: If the upload server returns an unexpected response.
        """
        self.buffer.seek(0)
        data = self.buffer.getvalue()
        head = {}
        length = len(data)
        if final and self.autocommit:
            if length:
                # pyrefly: ignore [unsupported-operation]
                part = "%i-%i" % (self.offset, self.offset + length - 1)
                # pyrefly: ignore [unsupported-operation]
                head["Content-Range"] = "bytes %s/%i" % (part, self.offset + length)
            else:
                # closing when buffer is empty
                head["Content-Range"] = "bytes */%i" % self.offset
                data = None
        else:
            head["Content-Range"] = "bytes %i-%i/*" % (
                self.offset,
                # pyrefly: ignore [unsupported-operation]
                self.offset + length - 1,
            )
        head.update(
            {"Content-Type": "application/octet-stream", "Content-Length": str(length)}
        )
        req = self.fs.files._http.request
        head, body = req(
            # pyrefly: ignore [unsupported-operation]
            self.location + "&supportsAllDrives=true",
            method="PUT",
            body=data,
            headers=head,
        )
        status = int(head["status"])
        assert status < 400, "Init upload failed"
        if status in [200, 201]:
            # server thinks we are finished - this should happen
            # only when closing
            blob = json.loads(body.decode())
            self.file_id = blob["id"]
            par = self.fs._parent(self.path)
            # duplicate should not happen here, and parent should already exist
            info = _finfo_from_response(blob, path_prefix=par)
            info["size"] = self.tell()
            if par in self.fs.dircache:
                self.fs.dircache[par].append(info)
        elif "range" in head:
            assert status == 308
        else:
            raise IOError

    @override
    def commit(self) -> None:
        """Finalize the upload when ``autocommit`` is False."""
        self.autocommit = True
        self._upload_chunk(final=True)

    @override
    def _initiate_upload(self) -> None:
        """Start a resumable upload session for a new file."""
        parent_id = self.fs.info(self.fs._parent(self.path))["id"]
        head = {"Content-Type": "application/json; charset=UTF-8"}
        # also allows description, MIME type, version, thumbnail...
        body = json.dumps(
            {"name": self.path.rsplit("/", 1)[-1], "parents": [parent_id]}
        ).encode()
        req = self.fs.files._http.request  # partial with correct creds
        # TODO : this creates a new file. If the file exists, you should
        #   update it by getting the ID and using PATCH, or delete and recreate,
        #   else you get two identically-named files
        r = req(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&supportsAllDrives=true",
            method="POST",
            headers=head,
            body=body,
        )
        head = r[0]
        assert int(head["status"]) < 400, "Init upload failed"
        self.location = r[0]["location"]

    @override
    def discard(self) -> None:
        """Cancel an in-progress resumable upload."""
        if self.location is None:
            LOGGER.debug("Abort file creation %s", self.path)
            return
        LOGGER.debug("Cancel file creation %s", self.path)
        uid = re.findall("upload_id=([^&=?]+)", self.location)
        head, _ = self.fs._call(
            "DELETE",
            "https://www.googleapis.com/upload/drive/v3/files",
            params={"uploadType": "resumable", "upload_id": uid},
        )
        assert int(head["status"]) < 400, "Cancel upload failed"
