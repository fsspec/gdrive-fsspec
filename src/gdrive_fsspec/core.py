import json
import logging
import os
import re
import warnings
from functools import cached_property
from typing import Any, Literal

from fsspec.spec import AbstractBufferedFile, AbstractFileSystem
from google.auth.credentials import AnonymousCredentials, Credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .typing_utils import override

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
    f: dict[str, Any], path_prefix: str | None = None
) -> dict[str, Any]:
    # strictly speaking, other types might be capable of having children,
    # such as packages
    # TODO: check specifically for links
    ftype = "directory" if f.get("mimeType") == DIR_MIME_TYPE else "file"
    if path_prefix:
        name = _normalize_path(path_prefix, f["name"])
    else:
        name = f["name"]
    info = {"name": name.lstrip("/"), "size": int(f.get("size", 0)), "type": ftype}
    f.update(info)
    return f


class MultipleFilesError(FileNotFoundError):
    pass


AuthMethod = Literal["anon", "browser", "cache", "service_account"]
ROOT_ID = "root"


class GoogleDriveFileSystem(AbstractFileSystem):
    """
    Access to google-drive as a file-system. In the google drive API,
    everything is a file resource. Folders are files with a special MIME type.

    Limitations:
    - we assume that each path identifies a unique file. In gdrive, it is
      possible to have multiple identically named files, and this will
      result in errors in this implementation.
    """

    protocol = "gdrive"
    root_marker = ""

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
        """
        Args:
            root_file_id (str or None): Folder file ID to use as the filesystem root (the empty path ``""``).
                Obtain it from a folder URL such as ``https://drive.google.com/drive/folders/<id>``.
                If omitted, defaults to the shared-drive root when ``drive`` is set, otherwise ``"root"``
                (the authenticated user's My Drive). A shared-drive ID is also accepted here for
                backwards compatibility (when it cannot be resolved as a file, it is treated as a
                shared drive and ``drive`` is set from it), but this is a legacy path; prefer passing
                ``drive`` to target a shared drive.
            token (str): One of "anon", "browser", "cache", "service_account".
                Using "browser" will prompt a URL to be put in a browser, and
                cache the response for future use with token="cache". "browser"
                will remove any previously cached token file, if it exists.
            access (str): One of "full_control", "read_only".
            spaces (str): Category of files to search; can be 'drive',
                'appDataFolder' and 'photos'. Of these, only the first is general.
            creds (dict or None): Required for "service_account" token.
                A dict with the service account credentials from the GCP console (same
                content as the downloaded JSON). See https://cloud.google.com/iam/docs/service-account-creds#key-types
                Files must be shared with the service account email from that JSON.
            drive (str or None): A shared-drive ID to scope API calls to. Resolved to a shared-drive ID via ``drives.list``; not
                a raw drive ID. Required for service-account uploads. If omitted,
                operations use the user's My Drive (or anonymous public files when ``token="anon"``).
                Combine with ``root_file_id`` to start below the shared-drive root, e.g. a subfolder ID inside that drive.
            auth_kwargs (dict or None): Additional keyword arguments passed to
                the authentication backend (``pydata_google_auth.get_user_credentials`` for user OAuth, or
                ``service_account.Credentials.from_service_account_info`` for service accounts).
                For headless or remote environments where a local callback server is unavailable, pass
                ``use_local_webserver=False`` to request a token via the console.
            **kwargs: Passed to parent.
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
            self.drive = self._drive_id_from_name(drive)

        if root_file_id and root_file_id != ROOT_ID:
            self._validate_root_file_id(root_file_id)

        self.root_file_id = root_file_id or self.drive or ROOT_ID

    def _validate_root_file_id(self, root_file_id: str) -> None:
        try:
            meta = self.files.get(
                fileId=root_file_id,
                fields="id,trashed,mimeType",
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

    def _confirm_shared_drive_root(self, drive_id: str) -> None:
        """Accept a shared-drive ID passed as ``root_file_id`` (legacy).

        Older versions documented ``root_file_id`` as accepting a "share, drive or
        folder ID", so a shared-drive ID may be passed here. When the ID does not
        resolve as a file, fall back to treating it as a shared drive and set
        ``self.drive`` from it so directory listings are scoped correctly. Prefer
        passing ``drive`` instead.
        """
        try:
            self.service.drives().get(driveId=drive_id).execute()
        except HttpError as err:
            if err.status_code == 404:
                raise FileNotFoundError(f"root_file_id {drive_id!r} not found") from err
            raise
        if self.drive is None:
            self.drive = drive_id
        # TODO(follow-up, issue #2): when self.drive is already set to a different
        # drive, root_file_id and _drive_kw() scope to conflicting drives. This is
        # pre-existing behaviour; raise on the conflict instead of silently ignoring.

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
    def srv(self) -> Any:
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
    def drives(self) -> list[Any]:
        """Drives accessible to the current user"""
        out: list[Any] = []
        page_token = None
        while True:
            # pyrefly: ignore [bad-argument-type]
            ret = self.service.drives().list(pageToken=page_token).execute()
            out.extend(ret["drives"])
            page_token = ret.get("nextPageToken")
            if page_token is None:
                break
        return out

    @override
    def mkdir(self, path: str, create_parents: bool = True, **kwargs: Any) -> Any:
        if create_parents and self._parent(path):
            self.makedirs(self._parent(path), exist_ok=True)
        par = self._parent(path)
        parent_id = self.info(par)["id"]
        meta = {
            "name": path.rstrip("/").rsplit("/", 1)[-1],
            "mimeType": DIR_MIME_TYPE,
            "parents": [parent_id],
        }
        if self.exists(path):
            raise FileExistsError(path)
        LOGGER.debug(f"Creating {path}, child of {parent_id}")
        out = self.files.create(body=meta, supportsAllDrives=True).execute()
        if par in self.dircache:
            # pyrefly: ignore [bad-argument-type]
            self.dircache[par].append(_finfo_from_response(out, path_prefix=par))
        self.dircache[path] = []
        return out

    @override
    def makedirs(self, path: str, exist_ok: bool = True) -> None:
        parts = path.split("/")
        path = ""
        for i, part in enumerate(parts):
            path = path + "/" + part if path else part
            if not self.exists(path):
                self.mkdir(path, create_parents=False)
            elif i == len(parts) - 1 and not exist_ok:
                raise FileExistsError(path)

    @override
    # pyrefly: ignore [bad-override]
    def rm_file(self, path: str, file_id: str | None = None) -> None:
        file_id = file_id or self.info(path)["id"]
        LOGGER.debug(f"Removing {path}, file_id={file_id}")
        self.files.delete(fileId=file_id, supportsAllDrives=True).execute()
        par = self._parent(path)
        if par in self.dircache:
            listing = self.dircache[par]
            i = [i for i, li in enumerate(listing) if li["name"] == path][0]
            listing.pop(i)
        self.dircache.pop(path, None)

    @override
    def rm(
        self, path: str, recursive: bool = True, maxdepth: int | None = None
    ) -> None:
        if recursive is False and self.isdir(path) and self.ls(path):
            raise ValueError("Attempt to delete non-empty folder")
        self.rm_file(path)

    @override
    def rmdir(self, path: str) -> None:
        if not self.isdir(path):
            raise ValueError("Path is not a directory")
        self.rm(path, recursive=False)

    @override
    def invalidate_cache(self, path: str | None = None) -> None:
        if path is None:
            self.dircache.clear()
        else:
            self.dircache.pop(self._strip_protocol(path), None)
        super().invalidate_cache(path)

    def export(self, path: str, mime_type: str) -> Any:
        """Convert a google-native file to another format and download

        mime_type is something like "text/plain"
        """
        # pyrefly: ignore [missing-attribute]
        file_id = self.path_to_file_id(path)
        return self.files.export(
            fileId=file_id, mimeType=mime_type, supportsAllDrives=True
        ).execute()

    def _drive_id_from_name(self, name: str) -> str:
        drive = [_["id"] for _ in self.drives if _["name"] == name]
        if len(drive) == 0:
            raise ValueError(f"Drive name {drive} not found")
        elif len(drive) == 1:
            return drive[0]
        else:
            raise ValueError(f"Drive name {drive} refers to multiple shared drives")

    @override
    # pyrefly: ignore [bad-override]
    def ls(self, path: str, detail: bool = False, trashed: bool = False) -> list[Any]:
        stripped_path = self._strip_protocol(path)
        files = self._ls_from_cache(stripped_path)

        if files is None:
            # get parent ID
            if "/" in stripped_path:
                # pyrefly: ignore [missing-attribute]
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
                        # pyrefly: ignore [bad-argument-type]
                        path_prefix=stripped_path,
                    )
                    self.dircache[stripped_path] = files

        if detail:
            return files
        else:
            return sorted([f["name"] for f in files])

    @override
    # pyrefly: ignore [bad-override]
    def info(self, path: str, trashed: bool = False) -> dict[str, Any]:
        stripped_path = self._strip_protocol(path)
        if stripped_path == "":
            return {
                "name": stripped_path,
                "mimeType": DIR_MIME_TYPE,
                "type": "directory",
                "size": 0,
                "id": self.root_file_id,
            }
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
    ) -> list[Any]:
        all_files = []
        page_token = None
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
            response = self.files.list(
                q=query,
                spaces=self.spaces,
                fields=afields,
                # pyrefly: ignore [bad-argument-type]
                pageToken=page_token,
                orderBy="name",
                pageSize=1000,
                **kwargs,
            ).execute()
            for f in response.get("files", []):
                # pyrefly: ignore [bad-argument-type]
                all_files.append(_finfo_from_response(f, path_prefix))
            page_token = response.get("nextPageToken", None)
            if page_token is None:
                break
        return all_files

    @override
    # pyrefly: ignore [bad-override]
    def _open(self, path: str, mode: str = "rb", **kwargs: Any) -> "GoogleDriveFile":
        return GoogleDriveFile(self, path, mode=mode, **kwargs)


class GoogleDriveFile(AbstractBufferedFile):
    def __init__(
        self,
        fs: GoogleDriveFileSystem,
        path: str,
        mode: str = "rb",
        block_size: int = DEFAULT_BLOCK_SIZE,
        autocommit: bool = True,
        **kwargs: Any,
    ) -> None:
        """
        Open a file.

        Parameters
        ----------
        fs: instance of GoogleDriveFileSystem
        mode: str
            Normal file modes. Currently only 'wb' amd 'rb'.
        block_size: int
            Buffer size for reading or writing (default 5MB)
        """
        super().__init__(fs, path, mode, block_size, autocommit=autocommit, **kwargs)

        if mode == "wb":
            self.location = None
        else:
            self.file_id = fs.info(path)["id"]
            self._media_object: Any | None = None

    @override
    def _fetch_range(self, start: int | None = None, end: int | None = None) -> bytes:
        """Get data from Google Drive

        start, end : None or integers
            if not both None, fetch only given range
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
    # pyrefly: ignore [bad-override]
    def _upload_chunk(self, final: bool = False) -> bool:
        """Write one part of a multi-block file upload

        Parameters
        ----------
        final: bool
            Complete and commit upload
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
        return True

    @override
    def commit(self) -> None:
        """If not auto-committing, finalize file"""
        self.autocommit = True
        self._upload_chunk(final=True)

    @override
    def _initiate_upload(self) -> None:
        """Create multi-upload"""
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
        """Cancel in-progress multi-upload"""
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
