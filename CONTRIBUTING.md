# Contributing to gdrive-fsspec

Thanks for your interest in contributing! This guide covers local setup, running tests, and the checks CI runs on pull requests.

## Prerequisites

Install `uv` and `pre-commit`:

- [`uv` install docs](https://docs.astral.sh/uv/getting-started/installation/)
- [`pre-commit` install docs](https://pre-commit.com/#install)

## Setup

1. Fork the repository on GitHub, then clone your fork:

   ```sh
   git clone git@github.com:<your-username>/gdrive-fsspec.git
   cd gdrive-fsspec
   ```

2. Create the virtual environment and install dependencies:

   ```sh
   uv sync --all-groups
   ```

3. Install pre-commit hooks:

   ```sh
   pre-commit install
   ```

## Running checks locally

CI runs the same checks you can run before pushing:

**Lint** (ruff, codespell, formatting, and more via pre-commit):

```sh
pre-commit run --all-files
```

**Type checking** (pyrefly):

```sh
uv run pyrefly check
```

**Unit tests** (default; no Google Drive credentials needed):

```sh
uv run pytest -v
```

**Coverage** (same unit tests; reports missing lines; CI requires at least 95%):

```sh
uv run pytest --cov=gdrive_fsspec --cov-report=term-missing
```

## Docstrings

Use reStructuredText (RST) double backticks for inline code in docstrings—for example, ``drive`` rather than `drive`. This matches the existing codebase and renders correctly with Sphinx-style tooling.

## Integration tests

Integration tests use a real Google Drive account and a directory named `gdrive_fsspec_testdir` on Drive.

```sh
uv run pytest -v -m integration
```

Set these environment variables before running them:

- `GDRIVE_FSSPEC_CREDENTIALS_PATH` — path to a service-account JSON, or the JSON string (starting with `{`). Required when using `service_account` (the default).
- `GDRIVE_FSSPEC_CREDENTIALS_TYPE` — token type (`service_account` default; use `cache` or `browser` for user OAuth).
- `GDRIVE_FSSPEC_DRIVE` — **Shared Drive name**. Required for service-account upload tests.

Service accounts cannot own files in Google Drive and have no storage quota. Uploads must target a [Shared Drive](https://developers.google.com/workspace/drive/api/guides/about-shareddrives) where the service account is a member with at least **Contributor** access. See [Google's storage-limit errors](https://developers.google.com/workspace/drive/api/guides/handle-errors#storage-limit).

For a personal Drive (no Shared Drive), use user OAuth instead: `GDRIVE_FSSPEC_CREDENTIALS_TYPE=cache` after a one-time `browser` login.

To run **all** tests (unit + integration), override the default marker filter:

```sh
uv run pytest -v -m ""
```

> **Note:** Integration tests do not run on PRs from forks, because those workflows cannot use repository secrets. They run on pushes to `master` and same-repo PRs. Google Drive has no good emulator; see [this discussion](https://github.com/fsspec/gdrive-fsspec/issues/23#issuecomment-2030367587).


### Creating a service account

1. Enable Google Drive API for the project in the Google Cloud Console.
2. Create a new service account.
3. Grant the service account permissions (likely Editor role, as there is no fine-grained Drive permissions).
4. Create a new key and download the JSON file.

5. To add a shared drive to the service account, get the service account email.
6. From the shared drive, add the service account email as a member with at least Contributor access.
