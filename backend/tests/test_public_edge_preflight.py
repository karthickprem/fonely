"""Behavioral tests for the credential-free public-edge preflight."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check-public-edge.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fonely_public_edge_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("value", ["", "changeme-secret", "api.example.in", "<secret>"])
def test_placeholder_or_empty_value_is_not_configured(value: str) -> None:
    module = _load_module()

    assert not module._is_configured(value)


def test_real_value_is_configured() -> None:
    module = _load_module()

    assert module._is_configured("api.fonely.invalid")


def test_required_missing_router_fails() -> None:
    module = _load_module()
    report = module.Report()

    module.check_router_gates({}, report, {"exotel"})

    assert report.failures == ["Exotel telephony is required but EXOTEL_WEBHOOK_SECRET is unset"]
    assert not report.warnings


def test_unselected_missing_router_is_not_run() -> None:
    module = _load_module()
    report = module.Report()

    module.check_router_gates({}, report, set())

    assert not report.failures
    assert len(report.not_checked_items) == 3


def test_required_placeholder_router_fails() -> None:
    module = _load_module()
    report = module.Report()

    module.check_router_gates(
        {"EXOTEL_WEBHOOK_SECRET": "changeme-exotel-webhook-secret"},
        report,
        {"exotel"},
    )

    assert report.failures == [
        "Exotel telephony is required but EXOTEL_WEBHOOK_SECRET is placeholder"
    ]
