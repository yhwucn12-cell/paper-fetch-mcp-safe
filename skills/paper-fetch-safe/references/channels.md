# Sources and routing

Checked 2026-10-01. Service limits are a conservative local policy and may differ from current provider limits. Response rate headers and `Retry-After` take precedence.

| Source | Use | Service pace | Documentation |
|---|---|---:|---|
| Europe PMC | PMCID PDF; PMID/DOI mapping; biomedical search | 10 requests/s | [REST API](https://europepmc.org/RestfulWebService) |
| arXiv | arXiv ID and preprint search through the official Atom API | 1 request/3 s, one connection | [API terms](https://info.arxiv.org/help/api/tou.html) |
| OpenAlex | DOI OA locations, search, 50 DOI batch groups | 10 requests/s local cap | [Authentication](https://help.openalex.org/api/authentication/), [pricing examples](https://help.openalex.org/access/example-costs/) |
| Unpaywall | DOI OA locations | 5 requests/s plus 100,000/day local cap | [Unpaywall access](https://help.openalex.org/access/unpaywall/) |
| Semantic Scholar | DOI OA PDF, up to 500 DOI batch | 1 request/s local cap | [Graph API](https://www.semanticscholar.org/product/api) |
| CORE | DOI repository copy, requires API key | 1 request/2 s local cap | [API v3](https://api.core.ac.uk/docs/v3) |
| OpenReview | Exact OpenReview note ID | 1 request/s local cap | [API v2](https://docs.openreview.net/reference/api-v2/openapi-definition) |
| ACL Anthology | Exact ACL paper ID | 1 request/s local cap | [FAQ](https://aclanthology.org/faq/) |
| PMLR | Exact volume/paper slug | 1 request/s local cap | [Proceedings](https://proceedings.mlr.press/) |
| bioRxiv / medRxiv | DOI through source record | 1 request/s local cap | [API](https://api.biorxiv.org/) |

Crossref supplies DOI metadata and never supplies an automatically downloaded publisher TDM link. [Crossref TDM guidance](https://www.crossref.org/documentation/retrieve-metadata/text-and-data-mining/).

OpenAlex currently permits a keyless daily budget but provides a larger free budget with an API key. Its work metadata lookup and hosted PDF endpoint have different prices; this server downloads OA location URLs, not OpenAlex hosted PDFs. Check the linked OpenAlex pages before large batches.
The server locally caps OpenAlex list requests at a $0.10/day keyless budget or $1/day with a key, estimating $0.0001 per list filter and $0.001 per search. Single-work DOI lookups are treated as free under the current documented pricing. `get_server_status` reports the remaining local estimate; provider response headers may impose a stricter limit.

## Licence evidence by source

Every candidate carries a licence verdict before it is fetched. Sources fall into three groups:

| Evidence | Sources | Meaning |
|---|---|---|
| `license` | any source that reports a CC licence (OpenAlex, Unpaywall, Europe PMC, HAL, Zenodo, OSF, bioRxiv/medRxiv) | explicit open licence; normalized and recorded |
| `source-attested` | arXiv, Europe PMC, PMC OA subset, OSF preprints, Zenodo open records, HAL, conference/preprint servers | the source only serves files it publishes openly |
| `oa-declared` | OpenAlex / Unpaywall / Semantic Scholar / CORE with an OA status but no licence | free to read, not necessarily redistributable; accepted under `license_policy = "open"` with a warning, refused under `strict` |

Aggregators are never source-attested: they relay third-party links, so a bare URL from them is refused. `license_policy = "any"` relaxes this and marks every such download `license_evidence = "none"`.

Sci-Hub is disabled by default (`allow_scihub = false`). Setting it to `true` logs a startup warning, registers the gray provider after all OA sources, paces it at one request per five seconds per mirror, and marks every result `gray`. Gray results are never produced under the default configuration.
