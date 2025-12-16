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

1. Service account credentials

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

2. OAuth with user credentials

 A browser will be opened to complete the OAuth authentication flow. Afterwards, the access
token will be stored locally, and you can reuse it in subsequent sessions.

```python
# use this the first time you run
token = 'browser'
# use this on subsequent attempts
# token = 'cache'
fs = GoogleDriveFileSystem(token=token)
 ```

3. Anonymous (read-only) access

If you want to interact with files that are shared publicly ("anyone with the link"),
then you do not need to authenticate to Google Drive.

```python
token = 'anon'
fs = GoogleDriveFileSystem(token=token)
```

See ``GoogleDriveFileSystem`` docstring for more details.

## Development

### Running tests

The tests require defining the following environment variables:
- gdrive_fsspec_CREDENTIALS_PATH: location of a credentials.json or the json blob itself
 (starting with "{")
- gdrive_fsspec_CREDENTIALS_TYPE: token type ("service_account" default)
- gdrive_fsspec_DRIVE: shared drive to use.

As can be seen, the assumption is, that a service account will be operating
on a shared drive. This requires the account to be have full shared access to
the drive.

You could use your own personal gdrive too, either by creating a credentials
JSON, or by using type "cache" following successful browser-based auth.

All tests take place in a directory "gdrive_fsspec_testdir".

```sh
pip install -e . pytest
pytest -v
```

### Style

Please run pre-commit before submitting PRs. You can automate this by
calling
```bash
$ pre-commit install
```
in the repo (once) before committing.

## Other implementations

- [PyDrive2](https://github.com/iterative/PyDrive2?tab=readme-ov-file#fsspec-filesystem) also provides an fsspec-compatible Google Drive API.
