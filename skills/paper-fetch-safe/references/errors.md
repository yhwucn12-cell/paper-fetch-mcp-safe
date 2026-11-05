# Result and error handling

| Code | Meaning | Response |
|---|---|---|
| `INVALID_REQUEST` | The structured `execute_paper_request` template is incomplete or mixes fields from different actions | Correct `action`, `query` or `identifiers`, and provide a download directory or configure `download_directory`. No download was started. |
| `BAD_IDENTIFIER` | Input has no supported scholarly identifier, or batch arguments are invalid | Check DOI, PMID, PMCID, arXiv ID, conference ID and year/limit values. |
| `PAYWALL_NO_OA` | All queried registered sources returned no PDF candidate at all | Try another identifier or source. |
| `LICENSE_RESTRICTED` | A PDF link existed but was refused: no open licence, no attesting source, or a gray source while `allow_scihub = false` | Treat as a licence limit. Do not retry hoping for a different answer; report it to the user. Relaxing `license_policy` or enabling `allow_scihub` is an operator decision with legal consequences. |
| `PROVIDER_UNAVAILABLE` | A source failed, a configured optional key is missing, or all candidate links failed | Check `get_server_status`; retry later or configure the relevant source. An OA absence is not established. |
| `RATE_LIMITED` | Source cooldown or quota; may include `retry_after` seconds | Wait at least that long before retrying. |
| `DOWNLOAD_CORRUPT` | Candidate response did not begin with `%PDF` | The server attempts later candidates and removes its temporary file. |

A `batch` response is a summary: `succeeded`, `failed`, `skipped`, a `failures` list of `{identifier, code}`, and `results_path` pointing at `<directory>/batch-results-<UTC>-<pid>-<n>.json` (the pid and counter keep concurrent batches from overwriting each other; the write is atomic), which holds every per-item record (`ok`, `skipped`, `path`, `sha256`, `provider`, `compliance_tier`, `error`). Refused-by-licence items are counted separately as `license_restricted` so they are not confused with `PAYWALL_NO_OA`. When any item came from a gray source the summary also carries `gray` (a count) and `warning`, so trimming never hides provenance. Duplicate identifiers and existing target paths are skipped rather than overwritten. If the results file cannot be written the full `items` are returned inline with a `note` instead. Successful items each have an adjacent `<file>.pdf.json` recording identifier, DOI, URL, license, version, tier, hash and UTC timestamp. Pass `verbosity="full"` to get the per-item records directly in `result`.
