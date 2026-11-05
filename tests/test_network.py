"""Opt-in live checks. Fill in the local paper-fetch.toml contact address."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from paper_fetch_mcp_safe.config import Config, ConfigError
from paper_fetch_mcp_safe.identifiers import parse_identifier
from paper_fetch_mcp_safe.models import ErrorCode, FetchError
from paper_fetch_mcp_safe.providers.hal import HalProvider
from paper_fetch_mcp_safe.providers.osf import OsfPreprintsProvider
from paper_fetch_mcp_safe.providers.pmc import PmcCloudProvider
from paper_fetch_mcp_safe.providers.zenodo import ZenodoProvider
from paper_fetch_mcp_safe.server import ServerState


pytestmark = pytest.mark.network


def state_or_skip():
    try:
        config = Config.from_file()
    except ConfigError as exc:
        if "mailto" in str(exc) or "configuration file not found" in str(exc):
            pytest.skip("fill in mailto in paper-fetch.toml for external API checks")
        raise
    return ServerState(replace(config, allow_scihub=False))


def test_open_access_doi_download(tmp_path):
    state = state_or_skip()
    result = state.download("10.1371/journal.pone.0141423", str(tmp_path))
    assert result["ok"], result
    pdf = Path(result["path"])
    assert pdf.read_bytes().startswith(b"%PDF")
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == result["sha256"]
    assert json.loads(Path(str(pdf) + ".json").read_text(encoding="utf-8"))["sha256"] == result["sha256"]
    assert result["compliance_tier"] == "oa"


def test_arxiv_direct_identifier_download(tmp_path):
    state = state_or_skip()
    result = state.download("arxiv:1706.03762", str(tmp_path))
    assert result["ok"], result
    assert Path(result["path"]).read_bytes().startswith(b"%PDF")
    assert result["provider"] == "arxiv"


def test_closed_doi_resolution_is_explicit(tmp_path):
    state = state_or_skip()
    result = state.download("10.1038/nature14539", str(tmp_path))
    # OA availability can change; a successful legal OA copy is also valid.
    if result["ok"]:
        assert result["compliance_tier"] == "oa"
    else:
        assert result["error"]["code"] in {"PAYWALL_NO_OA", "PROVIDER_UNAVAILABLE", "RATE_LIMITED", "DOWNLOAD_CORRUPT"}


@pytest.mark.parametrize("identifier,provider", [
    ("acl:2023.acl-long.1", "preprints"),
    ("pmlr:v235/sel24a", "preprints"),
])
def test_conference_exact_identifiers_resolve(identifier, provider):
    state = state_or_skip()
    result = state.resolve(identifier)
    assert any(item["provider"] == provider for item in result["candidates"]), result


@pytest.mark.parametrize("provider,identifier", [
    (HalProvider, "10.1080/00220388.2011.561330"),
    (ZenodoProvider, "10.5281/zenodo.1062358"),
    (PmcCloudProvider, "PMC10009416"),
])
def test_new_repository_downloads_verified_pdf(tmp_path, provider, identifier):
    state = state_or_skip()
    record = parse_identifier(identifier)
    try:
        candidates = provider(state.http).find_pdf(record)
        assert candidates, (provider.name, identifier)
        destination = tmp_path / f"{provider.name}.pdf"
        state.http.download_pdf(candidates[0], record, destination)
    except FetchError as exc:
        if isinstance(exc.__cause__, httpx.TransportError) or exc.code == ErrorCode.RATE_LIMITED:
            pytest.skip(f"source connection unavailable from this network: {exc}")
        raise
    assert destination.read_bytes().startswith(b"%PDF")


def test_osf_preprint_resolves_primary_pdf_candidate():
    state = state_or_skip()
    try:
        candidates = OsfPreprintsProvider(state.http).find_pdf(parse_identifier("osf:hqb2p_v1"))
    except FetchError as exc:
        if isinstance(exc.__cause__, httpx.TransportError) or exc.code == ErrorCode.RATE_LIMITED:
            pytest.skip(f"OSF API unavailable from this network: {exc}")
        raise
    assert candidates and candidates[0].url.startswith("https://osf.io/download/")


def test_elife_doi_now_uses_pmc_mapping():
    state = state_or_skip()
    result = state.resolve("10.7554/eLife.19274")
    assert result["paper"]["pmcid"] == "PMC5089862", result
    assert any(item["provider"] == "europepmc" for item in result["candidates"]), result
