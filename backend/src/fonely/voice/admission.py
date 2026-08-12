"""Tenant admission control and session capacity management.

Bounded concurrent sessions per tenant and global.  Integrates
with readiness gate — no sessions admitted before ready.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: str
    tenant_active: int = 0
    global_active: int = 0


class AdmissionController:
    """Thread-safe bounded session admission.

    Enforces per-tenant and global concurrent session limits.
    """

    def __init__(
        self,
        max_per_tenant: int = 10,
        max_global: int = 100,
    ) -> None:
        self._max_per_tenant = max_per_tenant
        self._max_global = max_global
        self._tenant_counts: dict[str, int] = {}
        self._global_count = 0
        self._lock = threading.Lock()
        self._total_admitted = 0
        self._total_rejected = 0

    def try_admit(self, tenant_id: str) -> AdmissionDecision:
        with self._lock:
            tenant_count = self._tenant_counts.get(tenant_id, 0)

            if self._global_count >= self._max_global:
                self._total_rejected += 1
                return AdmissionDecision(
                    admitted=False,
                    reason="global_capacity",
                    tenant_active=tenant_count,
                    global_active=self._global_count,
                )

            if tenant_count >= self._max_per_tenant:
                self._total_rejected += 1
                return AdmissionDecision(
                    admitted=False,
                    reason="tenant_capacity",
                    tenant_active=tenant_count,
                    global_active=self._global_count,
                )

            self._tenant_counts[tenant_id] = tenant_count + 1
            self._global_count += 1
            self._total_admitted += 1
            return AdmissionDecision(
                admitted=True,
                reason="admitted",
                tenant_active=tenant_count + 1,
                global_active=self._global_count,
            )

    def release(self, tenant_id: str) -> None:
        with self._lock:
            count = self._tenant_counts.get(tenant_id, 0)
            if count > 0:
                self._tenant_counts[tenant_id] = count - 1
                self._global_count = max(0, self._global_count - 1)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "global_active": self._global_count,
                "max_global": self._max_global,
                "max_per_tenant": self._max_per_tenant,
                "tenants_active": len([v for v in self._tenant_counts.values() if v > 0]),
                "total_admitted": self._total_admitted,
                "total_rejected": self._total_rejected,
            }
