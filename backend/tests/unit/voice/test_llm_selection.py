"""Provider-neutral LLM selection: config resolves, startup fails LOUD on
missing selected-provider config, and the pipeline-info descriptor derives from
the resolved config (never a literal, never a secret).

All config/fakes — no provider SDK, no network. The env is injected so nothing
reads os.environ.
"""

from __future__ import annotations

import pytest

from fonely.voice.config import LLMConfig
from fonely.voice.llm_selection import (
    LLMConfigError,
    resolve_llm_selection,
    resolved_llm_descriptor,
    validate_llm_startup,
)


class TestResolveFillsProviderDefaults:
    def test_anthropic_defaults_key_env_and_header(self):
        r = resolve_llm_selection(
            LLMConfig(provider="anthropic"), environ={"ANTHROPIC_API_KEY": "sk-x"}
        )
        assert r.provider == "anthropic"
        assert r.api_key_env == "ANTHROPIC_API_KEY"  # default filled
        assert r.auth_header_name == "x-api-key"  # default filled
        assert r.api_key_present is True

    def test_openai_compatible_takes_config_key_env_and_header(self):
        cfg = LLMConfig(
            provider="openai_compatible",
            model="gpt-5.6-luna",
            base_url="https://gw.example.com",
            api_key_env="GW_KEY",
            auth_header_name="Ocp-Apim-Subscription-Key",
        )
        r = resolve_llm_selection(cfg, environ={"GW_KEY": "abc"})
        assert r.api_key_env == "GW_KEY"
        assert r.auth_header_name == "Ocp-Apim-Subscription-Key"
        assert r.api_key_present is True

    def test_key_value_is_never_stored_on_resolved(self):
        # api_key_present is a bool; the resolved object exposes no field that
        # could carry the secret value.
        r = resolve_llm_selection(
            LLMConfig(provider="anthropic"), environ={"ANTHROPIC_API_KEY": "super-secret"}
        )
        assert "super-secret" not in repr(r)
        assert not hasattr(r, "api_key")

    def test_unknown_provider_raises(self):
        # provider is a Literal, but dynamic (env/JSON) values can still arrive;
        # resolution rejects an unknown provider rather than inventing defaults.
        cfg = LLMConfig(provider="anthropic")
        object.__setattr__(cfg, "provider", "mystery")  # simulate a bad dynamic value
        with pytest.raises(LLMConfigError, match="unknown LLM provider"):
            resolve_llm_selection(cfg, environ={})


class TestStartupFailsClosedOnMissingConfig:
    def test_missing_key_env_value_fails_startup(self):
        # Configured key env, but the env var is unset → refuse to start.
        with pytest.raises(LLMConfigError, match="unset or empty"):
            validate_llm_startup(LLMConfig(provider="anthropic"), environ={})

    def test_openai_compatible_without_key_env_fails(self):
        # No api_key_env and no default for openai_compatible → cannot auth.
        with pytest.raises(LLMConfigError, match="no api_key_env"):
            validate_llm_startup(
                LLMConfig(provider="openai_compatible", base_url="https://gw.example.com"),
                environ={},
            )

    def test_openai_compatible_without_base_url_fails(self):
        with pytest.raises(LLMConfigError, match="requires base_url"):
            validate_llm_startup(
                LLMConfig(
                    provider="openai_compatible",
                    api_key_env="GW_KEY",
                    base_url="",
                ),
                environ={"GW_KEY": "abc"},
            )

    def test_openai_compatible_non_https_base_url_fails(self):
        with pytest.raises(LLMConfigError, match="valid https URL"):
            validate_llm_startup(
                LLMConfig(
                    provider="openai_compatible",
                    api_key_env="GW_KEY",
                    base_url="http://gw.example.com",  # not https
                ),
                environ={"GW_KEY": "abc"},
            )

    def test_valid_openai_compatible_passes(self):
        resolved = validate_llm_startup(
            LLMConfig(
                provider="openai_compatible",
                model="gpt-5.6-luna",
                api_key_env="GW_KEY",
                base_url="https://gw.example.com",
                auth_header_name="Ocp-Apim-Subscription-Key",
            ),
            environ={"GW_KEY": "abc"},
        )
        assert resolved.provider == "openai_compatible"
        assert resolved.api_key_present is True

    def test_valid_anthropic_passes(self):
        resolved = validate_llm_startup(
            LLMConfig(provider="anthropic"), environ={"ANTHROPIC_API_KEY": "sk-x"}
        )
        assert resolved.provider == "anthropic"

    def test_unselected_provider_may_be_unconfigured(self):
        # "available but not selected": anthropic env being empty does NOT fail
        # startup when openai_compatible is the SELECTED provider.
        resolved = validate_llm_startup(
            LLMConfig(
                provider="openai_compatible",
                api_key_env="GW_KEY",
                base_url="https://gw.example.com",
            ),
            environ={"GW_KEY": "abc"},  # note: ANTHROPIC_API_KEY absent
        )
        assert resolved.provider == "openai_compatible"


class TestDescriptorDerivesFromResolvedConfig:
    def test_descriptor_reports_model_from_config_not_a_literal(self):
        d = resolved_llm_descriptor(
            LLMConfig(
                provider="openai_compatible",
                model="gpt-5.6-luna",
                base_url="https://gw.example.com",
                api_key_env="GW_KEY",
            ),
            environ={"GW_KEY": "abc"},
        )
        assert d["provider"] == "openai_compatible"
        assert d["model"] == "gpt-5.6-luna"  # from config, cannot drift

    def test_descriptor_reports_host_not_full_url_and_no_secret(self):
        d = resolved_llm_descriptor(
            LLMConfig(
                provider="openai_compatible",
                model="m",
                base_url="https://gw.example.com/v1?token=leak",
                api_key_env="GW_KEY",
                auth_header_name="X-Key",
            ),
            environ={"GW_KEY": "super-secret"},
        )
        assert d["gateway_host"] == "gw.example.com"  # host only
        # No secret value, and not the full URL (which carried a query string).
        assert "super-secret" not in str(d)
        assert "leak" not in str(d)
        assert d["auth_header_name"] == "X-Key"  # name, not value
        assert d["api_key_present"] is True

    def test_descriptor_project_default_model_marker(self):
        d = resolved_llm_descriptor(
            LLMConfig(provider="anthropic"), environ={"ANTHROPIC_API_KEY": "sk-x"}
        )
        assert d["model"] == "<project-default>"  # empty model reads clearly

    def test_descriptor_flags_missing_key(self):
        d = resolved_llm_descriptor(LLMConfig(provider="anthropic"), environ={})
        assert d["api_key_present"] is False  # absence is visible, not hidden
