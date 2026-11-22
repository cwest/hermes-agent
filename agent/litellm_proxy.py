"""Whether a route is a LiteLLM proxy: by name, or for a local endpoint by what it serves.

A proxy on ``127.0.0.1:4000`` carries no ``litellm`` token in its host or provider id, so it is
probed once at LiteLLM's unauthenticated liveness route, which answers exactly ``"I'm alive!"``.
Other signals fail: ``/v1/models`` carries no LiteLLM marker, ``x-litellm-*`` headers only arrive
on completion responses (too late for the first request), and ``/model/info`` is admin-gated on
keyed deployments.
"""

from __future__ import annotations

import time

from utils import base_url_hostname

_LIVENESS_PATH = "/health/liveliness"
_LIVENESS_BODY = "I'm alive!"
_probe_cache: dict[str, tuple[bool, float]] = {}


def is_litellm_route(provider_lower: str, base_url: str) -> bool:
    """True when a route is a LiteLLM proxy: ``litellm`` as a whole delimited token (not
    substring) in the provider id or host (a path segment never qualifies), or a local endpoint
    that answers as LiteLLM."""
    if _has_litellm_token(provider_lower, ":-_/") or _has_litellm_token(base_url_hostname(base_url), ".-"):
        return True
    return is_litellm_proxy(base_url)


def _has_litellm_token(value: str, delimiters: str) -> bool:
    """True when ``value`` contains ``litellm`` as a whole delimited token."""
    if not value:
        return False
    return "litellm" in value.translate(str.maketrans(delimiters, " " * len(delimiters))).split()


def is_litellm_proxy(base_url: str) -> bool:
    """True when a LOCAL endpoint is a LiteLLM proxy. Hosted endpoints are never probed.

    Verdicts are cached like ``detect_local_server_type``'s: positive for an hour (and on disk),
    negative for five minutes in memory only, so a proxy that was still starting is picked up soon.
    """
    from agent import model_metadata as mm

    if not base_url or not mm.is_local_endpoint(base_url):
        return False
    server_url = mm._server_root(mm._normalize_base_url(base_url))
    cached = _probe_cache.get(server_url)
    if cached is not None:
        ttl = mm._ENDPOINT_PROBE_TTL_SECONDS if cached[0] else mm._ENDPOINT_PROBE_FAILURE_TTL_SECONDS
        if time.monotonic() - cached[1] < ttl:
            return cached[0]
    if mm._endpoint_blackholed(server_url):
        return False
    if mm._local_probe_disk_get("litellm", server_url) is True:
        _probe_cache[server_url] = (True, time.monotonic())
        return True
    result = _probe_liveness(server_url, mm.model_metadata_http.resolve_verify(base_url))
    _probe_cache[server_url] = (result, time.monotonic())
    if result:
        mm._local_probe_disk_put("litellm", server_url, True)
    return result


def _probe_liveness(server_url: str, verify) -> bool:
    """One GET of the liveness route; a connect timeout blackholes the host for later probes."""
    import httpx

    from agent.model_metadata import _note_if_connect_timeout

    try:
        with httpx.Client(timeout=2.0, verify=verify) as client:
            response = client.get(server_url + _LIVENESS_PATH)
            return response.status_code == 200 and response.json() == _LIVENESS_BODY
    except (httpx.HTTPError, ValueError) as exc:
        _note_if_connect_timeout(exc, server_url)
        return False
