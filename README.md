# Google Drive fsspec implementation

This is an implementation of the fsspec interface for Google Drive.

This software is in beta stage and should not be relied upon in production settings.

## Installation

You can install it with pip from pypi or directly from source:

```sh
pip install gdrive_fsspec
pip install git+https://github.com/fsspec/gdrive-fsspec
```

## Usage

As gdrivefs implements the fsspec interface, most documentation can be found at https://filesystem-spec.readthedocs.io/en/latest/usage.html.

### Authentication

There are several methods to authenticate gdrivefs against Google Drive.

#### 1. Service account credentials

In this method, you provide a dict containing the service account credentials obtained
in the GCP console. The dict content is the same as the JSON file downloaded from the GCP console.
More details can be found here: <https://cloud.google.com/iam/docs/service-account-creds#key-types>.
This credential can be useful
when integrating with other GCP services, and when you don't want the user to
be prompted to authenticate.

```python
from gdrive_fsspec import GoogleDriveFileSystem
fs = GoogleDriveFileSystem(creds=service_account_credentials,
                           token="service_account")
```

#### 2. OAuth with user credentials

A browser will be opened to complete the OAuth authentication flow. Afterwards, the access
token will be stored locally, and you can reuse it in subsequent sessions.

```python
# use this the first time you run
token = 'browser'
# use this on subsequent attempts
# token = 'cache'
fs = GoogleDriveFileSystem(token=token)
```

On headless or remote machines (SSH sessions, containers, CI, and similar environments),
you may not be able to bind a local callback server or open a browser on the same host.
In that case, pass `use_local_webserver: False` in `auth_kwargs` to request a token via
the console.

```python
fs = GoogleDriveFileSystem(
    token=token,
    auth_kwargs={'use_local_webserver': False},
)
```

#### 3. Anonymous (read-only) access

If you want to interact with files that are shared publicly ("anyone with the link"),
then you do not need to authenticate to Google Drive.

```python
token = 'anon'
fs = GoogleDriveFileSystem(token=token)
```

See ``GoogleDriveFileSystem`` docstring for more details.

## Development

### Setup

1. Install `uv` and `pre-commit`:

- [uv install docs](https://docs.astral.sh/uv/getting-started/installation/)
- [pre-commit install docs](https://pre-commit.com/#install)

2. Clone your fork and `cd` into the repo:

```sh
git clone git@github.com:<your username>/gdrive-fsspec.git
cd gdrive-fsspec
```

3. Set up the environment:
```sh
uv sync
pre-commit install
```

### Running tests

There are unit tests and integration tests. Integration tests use a directory
named `gdrive_fsspec_testdir` on Google Drive.

**Unit tests** mock the Google Drive API and need no credentials. By default,
`pytest` runs unit tests only:

```sh
uv run pytest -v
```

**Integration tests** hit a real Google Drive account:

```sh
uv run pytest -v -m integration
```

Set these environment variables before running them:

- `GDRIVE_FSSPEC_CREDENTIALS_PATH` — path to a service-account JSON, or the JSON string (starting with `{`). Required when using `service_account` (the default).
- `GDRIVE_FSSPEC_CREDENTIALS_TYPE` — token type (`service_account` default; use `cache` or `browser` for user OAuth).
- `GDRIVE_FSSPEC_DRIVE` — **Shared Drive name**. Required for service-account upload tests.

Service accounts cannot own files in Google Drive and have no storage quota.
Uploads must target a [Shared Drive](https://developers.google.com/workspace/drive/api/guides/about-shareddrives) where the service account is a member with at least **Contributor** access.
See [Google’s storage-limit errors](https://developers.google.com/workspace/drive/api/guides/handle-errors#storage-limit).

For a personal Drive (no Shared Drive), use user OAuth instead:
`GDRIVE_FSSPEC_CREDENTIALS_TYPE=cache` after a one-time `browser` login.

To run all tests, override the default marker filter:

```sh
uv run pytest -v -m ""
```

> **Note:** Integration tests do not run on PRs from forks, because those
> workflows cannot use repository secrets. They run on pushes to `master` and same repo PRs.
> Google Drive has no good emulator; see [link](https://github.com/fsspec/gdrive-fsspec/issues/23#issuecomment-2030367587).

## Other implementations

- [PyDrive2](https://github.com/iterative/PyDrive2?tab=readme-ov-file#fsspec-filesystem) also provides an fsspec-compatible Google Drive API.
