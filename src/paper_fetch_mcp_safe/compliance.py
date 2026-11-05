"""Copyright-safety policy for PDF candidates.

Every candidate is judged on two independent axes before a single byte is written:

* **tier** — ``oa`` (open access) or ``gray`` (sources that redistribute in-copyright
  works without a licence). Gray is off unless the operator opts in.
* **licence evidence** — an explicit open licence on the candidate, or a provider that
  only ever serves material its own terms make openly available (arXiv, the PMC OA
  subset, Zenodo open records, …).

A candidate with neither is refused. That refusal is deliberately the default: an
aggregator URL is not evidence that redistribution is permitted, and "it downloaded
fine" is not a licence.

This module is pure — no network, no filesystem — so the policy can be tested and
reasoned about on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import ErrorCode, FetchError, PdfCandidate

POLICY_VERSION = "1"

# Providers whose API only exposes files the source itself publishes as open: arXiv
# preprints, the PMC/Europe PMC open-access subset, public OSF preprints, Zenodo records
# marked open, HAL deposit files, and the conference/preprint servers that host author
# copies. Aggregators (OpenAlex, Unpaywall, Semantic Scholar, CORE) are absent on
# purpose: they relay third-party links and must carry an explicit licence.
SOURCE_ATTESTED_PROVIDERS = frozenset({
    "arxiv",
    "europepmc",
    "pmc_cloud",
    "osf_preprints",
    "zenodo",
    "hal",
    "preprints",
})

LICENSE_POLICIES = ("strict", "open", "any")
GRAY_WARNING = "Gray source: review local law and institutional policy"
UNVERIFIED_WARNING = "Licence not verified: operator enabled license_policy = \"any\""
OA_DECLARED_WARNING = "No licence recorded: source reports open access, not a licence"

# OpenAlex and Unpaywall publish an oa_status; treat the ones that assert free
# availability as "OA declared". "closed" and missing values assert nothing.
OA_STATUSES = frozenset({"gold", "green", "hybrid", "bronze", "diamond", "oa", "open"})

_CC0_NAMES = {"cc0", "cc-0", "cc-zero", "public-domain", "publicdomain", "pd", "pdm"}
_OPEN_CLASSES = {
    "cc0": "public-domain",
    "cc-by": "open",
    "cc-by-sa": "open-sharealike",
    "cc-by-nc": "open-nc",
    "cc-by-nd": "open-nd",
    "cc-by-nc-sa": "open-nc-sa",
    "cc-by-nc-nd": "open-nc-nd",
}
_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def normalize_license(value: str | None) -> str | None:
    """Map the many spellings of a Creative Commons licence onto one short id.

    ``"CC BY 4.0"``, ``"cc_by"``, ``"https://creativecommons.org/licenses/by/4.0/"``
    and ``"cc-by"`` all become ``"cc-by"``. Anything unrecognised stays unrecognised —
    guessing here would turn a missing licence into an invented permission.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    if "publicdomain/zero" in text or "publicdomain/mark" in text:
        return "cc0"
    text = re.sub(r"^https?://(?:www\.)?creativecommons\.org/(?:licenses|publicdomain)/", "", text)
    text = text.split("?", 1)[0].strip("/")
    tokens = [token for token in _SPLIT_RE.split(text) if token]
    if not tokens:
        return None
    if tokens[0] in {"cc0", "0"} or (tokens[0] == "cc" and "0" in tokens[:2]):
        return "cc0"
    if tokens[0] in {"public", "pd"} or "publicdomain" in tokens or "pdm" in tokens:
        return "cc0"
    if tokens[0] == "cc":
        tokens = tokens[1:]
    if not tokens or tokens[0] != "by":
        return None
    parts = ["cc", "by"]
    for token in tokens[1:]:
        if token in {"nc", "nd", "sa"} and token not in parts:
            parts.append(token)
        elif token.isdigit():
            break
    return "-".join(parts)


def license_class(normalized: str | None) -> str:
    """Coarse class used in sidecars and tool output."""
    if normalized is None:
        return "unknown"
    return _OPEN_CLASSES.get(normalized, "unknown")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    tier: str
    license: str | None
    license_class: str
    evidence: str
    reason: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "tier": self.tier,
            "license": self.license,
            "license_class": self.license_class,
            "evidence": self.evidence,
            "reason": self.reason,
            "warnings": list(self.warnings),
        }


def evaluate(candidate: PdfCandidate, *, allow_gray: bool = False,
             license_policy: str = "open") -> PolicyDecision:
    """Decide whether one candidate may be downloaded, and on what evidence."""
    tier = (candidate.compliance_tier or "oa").lower()
    normalized = normalize_license(candidate.license)
    klass = license_class(normalized)

    if tier == "gray":
        if not allow_gray:
            return PolicyDecision(False, tier, normalized, klass, "gray",
                                  "gray sources are disabled (allow_scihub = false)")
        if license_policy == "strict":
            # Two independent switches guard gray: enabling the source is not enough
            # under the strict policy, which only accepts licensed or attesting sources.
            return PolicyDecision(False, tier, normalized, klass, "gray",
                                  "license_policy = strict refuses gray sources even when allow_scihub = true")
        return PolicyDecision(True, tier, normalized, klass, "gray", None, (GRAY_WARNING,))

    if tier != "oa":
        return PolicyDecision(False, tier, normalized, klass, "none",
                              f"unknown compliance tier: {tier}")

    if klass != "unknown":
        warnings = tuple(
            warning for condition, warning in (
                (klass in {"open-nc", "open-nc-sa", "open-nc-nd"}, "Non-commercial licence: confirm intended use"),
                (klass in {"open-nd", "open-nc-nd"}, "No-derivatives licence: redistribution of modified copies is not permitted"),
            ) if condition
        )
        return PolicyDecision(True, tier, normalized, klass, "license", None, warnings)

    if license_policy == "any":
        return PolicyDecision(True, tier, normalized, klass, "none", None, (UNVERIFIED_WARNING,))

    if candidate.provider in SOURCE_ATTESTED_PROVIDERS:
        return PolicyDecision(True, tier, normalized, klass, "source-attested")

    declared = (candidate.oa_status or "").lower()
    if license_policy == "open" and declared in OA_STATUSES:
        warnings = [OA_DECLARED_WARNING]
        if declared == "bronze":
            warnings.append("Bronze OA: free to read on the publisher site, redistribution rights unclear")
        return PolicyDecision(True, tier, normalized, klass, "oa-declared", None, tuple(warnings))

    return PolicyDecision(False, tier, normalized, klass, "none",
                          f"no open-licence evidence for provider {candidate.provider!r}")


def enforce(candidate: PdfCandidate, *, allow_gray: bool = False,
            license_policy: str = "open") -> PolicyDecision:
    """Evaluate and raise :class:`FetchError` when the candidate is not permitted."""
    decision = evaluate(candidate, allow_gray=allow_gray, license_policy=license_policy)
    if not decision.allowed:
        raise FetchError(ErrorCode.LICENSE_RESTRICTED, decision.reason or "candidate refused by licence policy")
    return decision
