# Verifying Google Drive API parameters

When adding or changing a Drive API call, **verify each parameter against the
live discovery document** before trusting it. Do not rely on:

- **Type stubs** (`googleapiclient-stubs`). Most methods are typed
  `def method(self, *, fileId, ..., **kwargs)`. The `**kwargs` swallows *any*
  keyword, so the stub accepting `supportsAllDrives=True` proves only that
  Python won't raise — not that the API supports the parameter.
- **Human docs / blog posts / memory.** They lag, omit per-method differences,
  and frequently copy a parameter (like `supportsAllDrives`) onto methods that
  don't actually take it.

The discovery document is the source of truth: the Python client is generated
from it, and it lists the exact parameters each method accepts.

## How to check

```bash
# Fetch once
curl -s "https://www.googleapis.com/discovery/v1/apis/drive/v3/rest" -o drive_v3.json

# List the accepted parameters for a given method, e.g. files.export
uv run python -c "
import json
d = json.load(open('drive_v3.json'))
m = d['resources']['files']['methods']['export']   # <resource>.methods.<method>
print(sorted(m.get('parameters', {}).keys()))
"
```

Swap `'files'` / `'export'` for the resource and method you care about
(`files.get`, `files.list`, `files.create`, `drives.list`, ...).

To see a parameter's type / required-ness / description:

```bash
uv run python -c "
import json
d = json.load(open('drive_v3.json'))
import pprint; pprint.pprint(d['resources']['files']['methods']['export']['parameters'])
"
```

## Worked example (the bug this guide came from)

```
files.export -> ['fileId', 'mimeType']                       # NO supportsAllDrives
files.get    -> ['acknowledgeAbuse', 'fileId', 'includeLabels',
                 'includePermissionsForView', 'supportsAllDrives',
                 'supportsTeamDrives']                         # has it
```

`files.export`/`export_media` accept **only** `fileId` and `mimeType`. Passing
`supportsAllDrives=True` was silently serialized as an ignored query param —
no error, mocked tests still passed — but it was wrong. `files.get` *does*
take it, which is why it's correct elsewhere in the code. Per-method
differences like this are exactly what the discovery doc catches and stubs hide.

## Checklist when touching a Drive call

1. Find the method: `resources.<resource>.methods.<method>` in the discovery doc.
2. Confirm **every** kwarg you pass is in that method's `parameters`.
3. If a kwarg isn't listed, remove it (it does nothing) or find the right method.
4. Remember mocked unit tests won't catch an unsupported-but-ignored param —
   the discovery doc is the only cheap pre-flight check.
