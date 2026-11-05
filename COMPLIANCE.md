# Copyright and licence boundary

This repository is a fork of `paper-fetch-mcp` that adds an enforceable copyright-safety
boundary. The fork keeps the original retrieval engine; what changes is **what it is
willing to download**. This document states the boundary, how it is enforced, and what it
does not do.

## What the software does

- Searches scholarly metadata (OpenAlex, arXiv, PubMed/Europe PMC, Crossref) and resolves
  identifiers (DOI, PMID, PMCID, arXiv, OpenReview, ACL, PMLR, HAL, Zenodo, OSF).
- Downloads **open-access PDFs** from sources that publish them, and writes a JSON sidecar
  next to every file recording provenance: provider, URL, licence, licence class, how the
  licence was established, the active policy version, and a SHA-256 checksum.

## The boundary

Two independent conditions are checked for every candidate URL before any bytes are
fetched:

1. **Compliance tier.** `oa` is the only tier accepted by default. `gray` sources
   (Sci-Hub mirrors) are refused unless the operator sets `allow_scihub = true`.
2. **Licence evidence.** At least one of:
   - an explicit open licence on the candidate (`CC0`, `CC BY`, `CC BY-SA`, `CC BY-NC`,
     `CC BY-ND`, `CC BY-NC-SA`, `CC BY-NC-ND`), parsed from the many spellings providers
     use and recorded normalized in the sidecar;
   - a **source-attesting provider** whose API only serves material that source publishes
     openly: `arxiv`, `europepmc`, `pmc_cloud`, `osf_preprints`, `zenodo`, `hal`,
     `preprints`;
   - for `license_policy = "open"` only, an OA designation from the provider
     (`oa_status` of `gold`/`green`/`hybrid`/`bronze`/`diamond`), recorded as
     `oa-declared` and warned about, because "free to read" is not a redistribution
     licence.

Aggregators — OpenAlex, Unpaywall, Semantic Scholar, CORE — are deliberately **not**
attesting sources. They relay third-party links, so they must carry an explicit licence
or an OA designation.

Refusals are reported, not hidden: the tool returns `LICENSE_RESTRICTED` with the reason,
`resolve` lists per-candidate refusals in `errors`, and `batch` counts them under
`license_restricted` so they are not confused with `PAYWALL_NO_OA`. When an unrelated
provider also failed, the answer is `PROVIDER_UNAVAILABLE` instead: the search was
incomplete, so the refusal is not presented as a final verdict. `search` output never
advertises a link the download path would refuse.

### Policy levels

| `license_policy` | Accepts | Use case |
|---|---|---|
| `strict` | explicit open licence, or an attesting source | strongest boundary; ignores OA designations and refuses gray even when `allow_scihub = true` |
| `open` (default) | the above, plus provider OA designations (warned) | normal research use |
| `any` | everything not gray | only when you supply your own legal basis |

### Enforcement points

The policy is not advisory:

- `compliance.evaluate()` is the single decision function; `compliance.enforce()` raises
  `LICENSE_RESTRICTED`.
- `router.resolve_candidates()` filters every provider's output.
- `ServerState.resolve()` / `download()` filter again, because record-level candidates and
  the deferred/gray passes bypass the router.
- `HttpClient.download_pdf()` re-evaluates (or takes the caller's decision) and refuses
  before opening a socket — so a direct caller cannot write a file without a recorded
  decision.
- The sidecar cannot be written without a decision, so every artifact on disk is
  auditable after the fact.

## What this does not do

- It does not determine whether *your* use of a work is permitted. Licences such as
  `CC BY-NC` restrict commercial use; `CC BY-ND` restricts derivative works; publisher
  "bronze" OA may permit reading but not redistribution. Warnings are emitted but the
  judgement is yours.
- It cannot verify that a provider's licence metadata is truthful. The sidecar records
  which provider asserted what, so a bad assertion is traceable rather than laundered.
- It does not remove the Sci-Hub provider source file. It stays in the tree as the
  auditable implementation of the refused path, is never constructed by the default
  registry, and is only reachable with an explicit opt-in that logs a warning at startup.
- It is not legal advice. Local copyright exceptions (private study, text and data mining,
  quotation) differ by jurisdiction; this software does not model them.

## Opting out of the boundary

Setting `allow_scihub = true` or `license_policy = "any"` is supported, logged as a
warning at startup, and reflected in `get_server_status().compliance` and in every
sidecar (`license_policy`, `license_evidence`). Results obtained that way are labelled so
they cannot be mistaken for policy-compliant ones. Do not enable either without checking
the law where you are and your institution's policy.

## Provenance of this fork

The local source project, `paper-fetch-mcp` v0.1.0, did not contain a public licence.
Its author is publishing this fork under the MIT licence in `LICENSE`. The fork renames
the distribution to `paper-fetch-mcp-safe` and the import package to
`paper_fetch_mcp_safe` so the two can be installed side by side, and drops the source
project's local test artifacts and mirror-specific run logs. See `NOTICE` for the list
of behavioural changes.
