# paper-fetch-mcp-safe

[中文](README.md) · [English](README_EN.md)

[![Tests](https://github.com/yhwucn12-cell/paper-fetch-mcp-safe/actions/workflows/tests.yml/badge.svg)](https://github.com/yhwucn12-cell/paper-fetch-mcp-safe/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

An MCP server for scholarly search, paper resolution, and open-PDF retrieval with an enforceable **copyright and licence boundary**.

- Search OpenAlex, arXiv, and PubMed / Europe PMC
- Resolve DOI, PMID, PMCID, arXiv, OpenReview, ACL, PMLR, HAL, Zenodo, and OSF identifiers
- Require open-licence or open-access evidence before downloading
- Validate PDFs, calculate SHA-256, and create auditable JSON sidecars
- Download by identifier, exact title, or batches of up to 500 papers

> Sci-Hub is disabled by default. See [COMPLIANCE.md](COMPLIANCE.md) for the complete policy and limitations.

## Quick start

### Windows PowerShell

```powershell
git clone https://github.com/yhwucn12-cell/paper-fetch-mcp-safe.git
cd paper-fetch-mcp-safe
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
Copy-Item paper-fetch.toml.example paper-fetch.toml
```

### macOS / Linux

```bash
git clone https://github.com/yhwucn12-cell/paper-fetch-mcp-safe.git
cd paper-fetch-mcp-safe
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp paper-fetch.toml.example paper-fetch.toml
```

Edit `paper-fetch.toml` and provide at least a contact email:

```toml
mailto = "you@example.org"
license_policy = "open"
allow_scihub = false
# download_directory = "downloads"
```

Start the server on Windows:

```powershell
.venv\Scripts\python.exe -m paper_fetch_mcp_safe.server --config paper-fetch.toml
```

On macOS / Linux:

```bash
.venv/bin/python -m paper_fetch_mcp_safe.server --config paper-fetch.toml
```

## MCP client configuration

Add the following to a client that supports stdio MCP. Replace the paths with absolute paths on your machine:

```json
{
  "mcpServers": {
    "paper-fetch-safe": {
      "command": "D:/path/to/paper-fetch-mcp-safe/.venv/Scripts/python.exe",
      "args": [
        "-m",
        "paper_fetch_mcp_safe.server",
        "--config",
        "D:/path/to/paper-fetch-mcp-safe/paper-fetch.toml"
      ]
    }
  }
}
```

The server exposes two tools:

| Tool | Purpose |
|---|---|
| `execute_paper_request` | Search, resolve, download one paper, or run a batch |
| `get_server_status` | Inspect providers, rate limits, and the active licence policy |

## Configuration

| Key | Default | Description |
|---|---:|---|
| `mailto` | required | Contact email sent to scholarly APIs |
| `download_directory` | unset | Default download directory; requests may also pass `directory` |
| `license_policy` | `open` | `strict`, `open`, or `any` |
| `allow_scihub` | `false` | Enable gray sources; startup emits a warning |
| `openalex_api_key` | empty | Optional OpenAlex API key |
| `s2_api_key` | empty | Optional Semantic Scholar API key |
| `core_api_key` | empty | Optional CORE API key; CORE is skipped when unset |
| `timeout_seconds` | `45.0` | HTTP timeout |
| `host_rates` | built in | Per-host request rates |

Licence policies:

| Policy | Accepted candidates |
|---|---|
| `strict` | Explicit open licence or a source-attested open repository; gray is always refused |
| `open` | Default; also accepts provider OA declarations and adds a warning |
| `any` | Accepts non-gray candidates without licence evidence; use only with your own legal basis |

## Usage

`execute_paper_request` accepts structured parameters. `accepted=true` means the request ran; the actual outcome is in `result`.

### Search

```json
{
  "action": "search",
  "query": "water oxidation catalyst",
  "source": "auto",
  "limit": 5,
  "year_min": 2022
}
```

### Resolve an open PDF

```json
{
  "action": "resolve",
  "identifiers": ["10.1021/jacs.4c00478"],
  "verbosity": "full"
}
```

### Download one paper

```json
{
  "action": "download",
  "identifiers": ["arxiv:1706.03762"],
  "directory": "D:/papers"
}
```

You can also download by exact title:

```json
{
  "action": "download",
  "query": "A Precisely Specified Article Title",
  "directory": "D:/papers"
}
```

A unique exact match downloads automatically. Ambiguous or missing matches return `needs_review=true`.

### Batch download

```json
{
  "action": "batch",
  "identifiers": [
    "10.1021/jacs.4c00478",
    "PMC10009416",
    "arxiv:1706.03762"
  ],
  "directory": "D:/papers",
  "concurrency": 4
}
```

A batch accepts 1–500 identifiers. The default response is a summary; per-item results are written to `batch-results-*.json` in the download directory.

## Download output

Each successful PDF has an adjacent `.pdf.json` sidecar containing:

- Provider, source URL, and scholarly identifier
- Original and normalized licence, licence class, and evidence type
- Active policy version, document version, timestamp, and SHA-256

Common errors:

| Code | Meaning |
|---|---|
| `PAYWALL_NO_OA` | No open-PDF candidate was found |
| `LICENSE_RESTRICTED` | A link existed but lacked sufficient licence evidence or was refused by policy |
| `PROVIDER_UNAVAILABLE` | A provider failed, so the result is incomplete |
| `RATE_LIMITED` | A provider rate-limited the request; may include `retry_after` |
| `DOWNLOAD_CORRUPT` | The response was not a valid PDF or exceeded 200 MiB |

## Testing

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q -m local_subprocess
.venv\Scripts\python.exe scripts\paper_download_test.py audit
```

Live-provider tests:

```powershell
.venv\Scripts\python.exe -m pytest -q -m network
.venv\Scripts\python.exe scripts\paper_download_test.py search resolve download batch
```

## Documentation

- [Copyright and licence boundary](COMPLIANCE.md)
- [Changes from the source project](NOTICE)
- [MCP skill](skills/paper-fetch-safe/SKILL.md)
- [Sources and rate limits](skills/paper-fetch-safe/references/channels.md)
- [Error handling](skills/paper-fetch-safe/references/errors.md)

## License

The code is released under the [MIT License](LICENSE). Third-party papers and APIs remain subject to their own licences and terms. This software is not legal advice.
