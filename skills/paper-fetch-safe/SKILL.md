---
name: paper-fetch-safe
description: Turn natural-language paper search and download requests into structured paper-fetch-mcp-safe tool calls. Use for titles, topics, DOI, PMID, PMCID, arXiv and conference identifiers, or batches up to 500 papers. Downloads are limited to open-licence and open-access sources.
---

# Paper fetch (licence-checked)

Use the `paper-fetch-mcp-safe` tools to search metadata and obtain PDF files with a source, licence evidence, SHA-256 checksum and JSON provenance sidecar.

The server enforces a copyright-safety boundary. A candidate is downloaded only when it is open access **and** carries either an explicit open licence or a source that only serves openly published files. Aggregator links with no licence evidence are refused with `LICENSE_RESTRICTED`. Do not try to work around that refusal by asking for a different identifier repeatedly; report it to the user as "no licence-verified open copy" instead.

## Standard request template

Interpret the user's natural-language instruction and fill the fields of `execute_paper_request` before calling it. The server accepts structured fields, not an unparsed instruction. Omit fields that do not apply:

```json
{
  "action": "search | resolve | download | batch",
  "query": "title or topic for search; exact title for download without an identifier",
  "identifiers": ["one exact identifier, or 1-500 for batch"],
  "directory": "absolute path on the MCP server, optional if download_directory is configured",
  "source": "auto | openalex | arxiv | pubmed, search or title download",
  "limit": 10,
  "year_min": null,
  "year_max": null,
  "concurrency": 4,
  "verbosity": "minimal | full, defaults to minimal",
  "review_search_results": false
}
```

| Natural-language intent | Filled fields |
|---|---|
| Find papers about a topic or by title | `action="search"`, `query=...`; add source, limit or years only when requested |
| Check whether one identified paper has a PDF | `action="resolve"`, `identifiers=[...]` |
| Download one identified paper | `action="download"`, `identifiers=[...]`, optional directory |
| Download a paper named by its title | `action="download"`, `query="exact title"`, optional directory; unique exact-title match downloads automatically |
| Download a supplied list of identifiers | `action="batch"`, `identifiers=[...]`, optional directory and concurrency |
| Need candidate URLs, licences or per-item records | add `verbosity="full"` to `resolve` or `batch`; omit it for the default summary |

Extract identifiers exactly from the instruction; never invent a DOI. DOI URLs, PMID, PMCID, arXiv, OpenReview, ACL, PMLR, HAL, Zenodo and OSF identifiers are accepted. A title or topic belongs in `query`, never in `identifiers`. For **download by exact title**, submit the download request directly: the server searches and automatically downloads a unique exact-title match. `review_search_results` defaults to `false`; set it to `true` only when the user requests review before download. If no unique exact match exists, the response has `needs_review=true` with candidates; resolve that ambiguity before downloading. For a broad topic, search first and choose papers according to the user's stated count and criteria, then submit a batch request without an extra review step when the choice is clear.

Examples:

```json
{"action":"download","identifiers":["10.1021/jacs.4c00478"]}
{"action":"download","query":"A Precisely Specified Article Title"}
{"action":"batch","identifiers":["10.1021/jacs.4c00478","10.1002/anie.201900650"],"directory":"D:/papers"}
{"action":"search","query":"water oxidation catalyst","source":"auto","limit":5,"year_min":2022}
```

Check `accepted` first: `false` means the template was rejected without running a search or download. If `accepted` is `true`, inspect `result` for actual outcomes; it does not imply a PDF was found. `needs_review=true` means the title could not be selected automatically or review was requested. Paths refer to the MCP server machine. `download_directory` may be set in `paper-fetch.toml` and is resolved relative to that file. If no directory is available for a download, request one from the user.

Responses are summaries by default. Report the counts, the failures and any `results_path`; read the results JSON or a `<file>.pdf.json` sidecar only when a specific field is needed, and never paste a full batch listing back into the conversation.

## Workflow

1. With a DOI, PMID, PMCID, arXiv ID, OpenReview ID, ACL ID or PMLR ID, use the standard template to resolve or download. DOI URLs are accepted. OpenReview, ACL and PMLR landing-page URLs are also accepted.
2. With an exact title, use `action="download"` and `query` for automatic exact-title selection. With a topic, search and choose papers by the user's criteria. Set `source="pubmed"` for biomedical queries and `source="arxiv"` for preprints.
3. For a list of 1–500 identifiers, use `action="batch"`. Inspect every item: batch success does not imply every PDF downloaded.
4. For more than 500 papers, use an official bulk dataset or snapshot appropriate to the source rather than sending repeated batches merely to bypass the cap. For a few hundred papers, consider the source's bulk option if it better fits the task.

The server only uses open-access sources. Sci-Hub mirrors are disabled unless the operator explicitly sets `allow_scihub = true`, which also logs a startup warning and marks results `gray`.

If a result is `PAYWALL_NO_OA`, no configured source returned a PDF at all; try another identifier or source. `LICENSE_RESTRICTED` is different and more specific: a PDF link existed but its licence could not be established, so it was refused. Report that as a licence limit, not as a network failure, and never imply the user can obtain the file through this server. `PROVIDER_UNAVAILABLE` and `RATE_LIMITED` mean the search was inconclusive; retry according to `retry_after` or check status. `DOWNLOAD_CORRUPT` means a candidate returned invalid PDF content and the server has attempted the next candidate.

Configure `paper-fetch.toml` in the server's working directory, or pass `--config /absolute/path/paper-fetch.toml` when starting it. Set `mailto` to a valid contact email. Optional file keys are `openalex_api_key`, `s2_api_key`, `core_api_key`, `download_directory`, `host_rates` (TOML table of hostnames to requests per second), `license_policy` (`strict`, `open` by default, or `any`), `allow_scihub` (defaults to `false`) and `scihub_mirrors`. Keep this local file out of version control; copy `paper-fetch.toml.example` to start. Call `get_server_status` for registered providers and host cooldowns.

See [channels](references/channels.md) for coverage, licence evidence and rate guidance, and [errors](references/errors.md) for result handling.
