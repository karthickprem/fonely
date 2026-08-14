"""Provider-neutral LLM selection: resolve config, validate at startup, and
describe the resolved choice without ever touching a secret value.

The runtime must be able to run an OpenAI-compatible model (a gateway, a
self-hosted endpoint, a third-party API) selected purely by config, with the
Anthropic path still available but not privileged. Three things live here, all
free of provider SDKs and network — so they are unit-testable against config
alone:

  * ``resolve_llm_selection`` — turn an ``LLMConfig`` into a fully-resolved
    ``ResolvedLlm`` (provider, model, base_url, the auth header NAME, and the
    key READ from the configured env var). It reads the key value only to know
    the key is present; the value is never returned or logged.
  * ``validate_llm_startup`` — fail LOUD at startup if the SELECTED provider is
    missing required config. A silent default that limps into a call with no
    key is the failure this prevents: the process must refuse to start, not
    surface the gap as a 500 mid-call.
  * ``resolved_llm_descriptor`` — the non-secret facts (provider, model,
    gateway host, header NAME) for ``/pipeline-info`` and any UI. Derived from
    the resolved config so what is reported can never drift from what is served.
    Never includes the key value, and reports the host of base_url, not the URL,
    so a query string can't leak.

No hardcoded provider env var or gateway header name appears here: the auth
header NAME and the env var NAME are themselves config. "anthropic" and
"openai_compatible" are the two known adapters; anthropic stays available but is
never auto-privileged over the neutral path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from .config import LLM_PROVIDERS, LLMConfig

# Per-provider conventional defaults, used ONLY when the config leaves a field
# empty. These are defaults, not hardcoding: any of them is overridable by
# config, and the value read is a NAME (env var / header), never a secret.
_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "anthropic": {
        "api_key_env": "ANTHROPIC_API_KEY",
        "auth_header_name": "x-api-key",
    },
    "openai_compatible": {
        # No universal default env var — an OpenAI-compatible endpoint could be
        # any gateway, so the config MUST name its key env and (usually) its
        # base_url. Left empty here so validation forces the operator to be
        # explicit rather than inheriting an accidental Anthropic key.
        "api_key_env": "",
        "auth_header_name": "Authorization",
    },
}


class LLMConfigError(RuntimeError):
    """The selected LLM provider is misconfigured. Raised at startup so the
    process refuses to run rather than failing on the first call."""


@dataclass(frozen=True)
class ResolvedLlm:
    """A fully-resolved LLM selection. ``api_key_present`` records that the
    configured key env var held a non-empty value AT RESOLUTION TIME — the value
    itself is deliberately absent so it cannot be logged or serialized."""

    provider: str
    model: str
    base_url: str
    api_key_env: str
    auth_header_name: str
    auth_header_format: str
    api_key_present: bool


def _defaults_for(provider: str) -> dict[str, str]:
    return _PROVIDER_DEFAULTS.get(provider, {})


def resolve_llm_selection(
    config: LLMConfig, *, environ: dict[str, str] | None = None
) -> ResolvedLlm:
    """Resolve ``config`` into a ``ResolvedLlm``, filling empty fields from the
    selected provider's conventional defaults.

    ``environ`` is injectable so tests drive it without touching os.environ. The
    API key VALUE is read only to set ``api_key_present``; it is never stored on
    the result. Raises ``LLMConfigError`` if the provider is unknown (an unknown
    provider cannot have conventional defaults, so resolving it would invent
    behavior)."""
    env = os.environ if environ is None else environ

    if config.provider not in LLM_PROVIDERS:
        raise LLMConfigError(
            f"unknown LLM provider {config.provider!r}; known providers: {sorted(LLM_PROVIDERS)}"
        )

    defaults = _defaults_for(config.provider)
    api_key_env = config.api_key_env or defaults.get("api_key_env", "")
    auth_header_name = config.auth_header_name or defaults.get("auth_header_name", "")

    api_key_present = bool(api_key_env) and bool(env.get(api_key_env, ""))

    return ResolvedLlm(
        provider=config.provider,
        model=config.model,
        base_url=config.base_url,
        api_key_env=api_key_env,
        auth_header_name=auth_header_name,
        auth_header_format=config.auth_header_format,
        api_key_present=api_key_present,
    )


def validate_llm_startup(
    config: LLMConfig, *, environ: dict[str, str] | None = None
) -> ResolvedLlm:
    """Validate the SELECTED provider's config at startup; return the resolved
    selection or raise ``LLMConfigError``.

    The fail-closed contract: if the selected provider is missing anything it
    needs to make a call, the process must refuse to start. Checks, per selected
    provider:

      * provider is known;
      * an API-key env var name is configured AND that env var holds a value
        (a configured-but-unset key is the classic "works in dev, 401 in prod"
        gap — caught here, not at call time);
      * an auth header name is resolved;
      * for ``openai_compatible``, a ``base_url`` is set and is a valid https
        URL (an OpenAI-compatible endpoint has nowhere to send without one; the
        native anthropic client supplies its own default base_url, so it is not
        required there).

    This validates only the SELECTED provider — an unselected adapter being
    unconfigured is fine (that is what "available but not selected" means)."""
    resolved = resolve_llm_selection(config, environ=environ)

    if not resolved.api_key_env:
        raise LLMConfigError(
            f"LLM provider {resolved.provider!r} has no api_key_env configured; "
            "set LLMConfig.api_key_env to the env var holding the key"
        )
    if not resolved.api_key_present:
        raise LLMConfigError(
            f"LLM provider {resolved.provider!r} selected but env var "
            f"{resolved.api_key_env!r} is unset or empty — refusing to start "
            "with a missing key rather than failing on the first call"
        )
    if not resolved.auth_header_name:
        raise LLMConfigError(f"LLM provider {resolved.provider!r} has no auth_header_name resolved")

    if resolved.provider == "openai_compatible":
        if not resolved.base_url:
            raise LLMConfigError(
                "openai_compatible provider requires base_url (the endpoint to "
                "call); none configured"
            )
        parsed = urlparse(resolved.base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise LLMConfigError(
                f"openai_compatible base_url must be a valid https URL, got {resolved.base_url!r}"
            )

    return resolved


def resolved_llm_descriptor(
    config: LLMConfig, *, environ: dict[str, str] | None = None
) -> dict[str, object]:
    """Non-secret description of the resolved LLM selection, for /pipeline-info
    and any UI. Derived from the resolved config so the reported model/provider
    can never drift from what would actually be served.

    Reports the base_url HOST (not the full URL, which could carry a query
    string), the auth header NAME (not its value), and whether the key is
    present — never the key itself. ``model`` falls back to a readable marker
    when the config leaves it as the project default."""
    resolved = resolve_llm_selection(config, environ=environ)
    host = ""
    if resolved.base_url:
        host = urlparse(resolved.base_url).hostname or ""
    return {
        "provider": resolved.provider,
        "model": resolved.model or "<project-default>",
        "gateway_host": host,
        "auth_header_name": resolved.auth_header_name,
        "api_key_env": resolved.api_key_env,
        "api_key_present": resolved.api_key_present,
    }
