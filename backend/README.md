# Provider and comparison foundations

This directory preserves the earlier Python standard-library implementation of public-data adapters, normalized event models, matching, comparison and snapshot tooling. It is separate from the dashboard demo and is not a current production deployment.

## Offline checks

Python 3.11+ is sufficient. From this directory:

```sh
# macOS / Linux
PYTHONPATH=src python -m unittest discover -s tests -v
```

```powershell
# PowerShell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

The tests use fixtures and mocked network calls. Live provider access, credentials, notifications and production operation are outside this demo. Do not run provider CLI commands unless you intend to make external requests.
