"""Priority-ordered candidate discovery with isolated provider failures."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import compliance
from .models import ErrorCode, FetchError, PaperRecord, PdfCandidate, redact_secrets
from .providers.arxiv import ArxivProvider
from .providers.base import Provider, unique_candidates
from .providers.core import CoreProvider
from .providers.europepmc import EuropePmcProvider
from .providers.hal import HalProvider
from .providers.openalex import OpenAlexProvider
from .providers.osf import OsfPreprintsProvider
from .providers.pmc import PmcCloudProvider
from .providers.preprints import PreprintProvider
from .providers.scihub import SciHubProvider
from .providers.semantic_scholar import SemanticScholarProvider
from .providers.unpaywall import UnpaywallProvider
from .providers.zenodo import ZenodoProvider


@dataclass(slots=True)
class ResolveOutcome:
    candidates: list[PdfCandidate] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def build_registry(http: HttpClient) -> list[Provider]:
    providers: list[Provider] = [
        EuropePmcProvider(http),
        ArxivProvider(http),
        OsfPreprintsProvider(http),
        PmcCloudProvider(http),
        OpenAlexProvider(http),
        HalProvider(http),
        ZenodoProvider(http),
        UnpaywallProvider(http),
        SemanticScholarProvider(http),
        CoreProvider(http),
        PreprintProvider(http),
    ]
    if http.config.allow_scihub:
        providers.append(SciHubProvider(http))
    return sorted(providers, key=lambda provider: provider.priority)


def resolve_candidates(
    record: PaperRecord,
    registry: list[Provider],
    *,
    include_gray: bool = False,
    prefetched: dict[str, dict[str, list[PdfCandidate]]] | None = None,
    skip_specialized: bool = False,
    deferred_only: bool = False,
    allow_gray: bool = False,
    license_policy: str = "open",
) -> ResolveOutcome:
    """Collect candidates, dropping any the licence policy refuses.

    Refusals are reported as errors rather than silently dropped: "this paper has no
    open copy" and "this link has no licence we can act on" are different answers.
    """
    outcome = ResolveOutcome()
    for provider in registry:
        if deferred_only and provider.name not in {"hal", "zenodo"}:
            continue
        if skip_specialized and provider.priority <= 6:
            continue
        if provider.compliance_tier == "gray" and (not include_gray or outcome.candidates):
            continue
        if not provider.supports(record):
            continue
        exact_repository = (provider.name == "hal" and bool(record.extra.get("hal_id"))) or (
            provider.name == "zenodo" and bool(record.extra.get("zenodo_id") or
                                                (record.doi or "").lower().startswith("10.5281/zenodo.")))
        if provider.name in {"hal", "zenodo"} and outcome.candidates and not exact_repository and not deferred_only:
            continue
        try:
            cached = (prefetched or {}).get(provider.name)
            if cached is not None and record.doi and record.doi.lower() in cached:
                candidates = cached[record.doi.lower()]
            else:
                candidates = provider.find_pdf(record)
        except FetchError as exc:
            outcome.errors.append({"provider": provider.name, **exc.to_dict()})
            continue
        except Exception as exc:
            outcome.errors.append({"provider": provider.name,
                                   **FetchError(ErrorCode.PROVIDER_UNAVAILABLE, redact_secrets(str(exc))).to_dict()})
            continue
        permitted = []
        for candidate in candidates:
            decision = compliance.evaluate(candidate, allow_gray=allow_gray,
                                           license_policy=license_policy)
            if decision.allowed:
                permitted.append(candidate)
            else:
                outcome.errors.append({"provider": provider.name,
                                       **FetchError(ErrorCode.LICENSE_RESTRICTED,
                                                    decision.reason or "candidate refused").to_dict()})
        outcome.candidates.extend(permitted)
        if permitted and provider.priority <= 6:
            # A direct repository identifier is more reliable than aggregator links.
            break
    outcome.candidates = unique_candidates(outcome.candidates)
    return outcome
