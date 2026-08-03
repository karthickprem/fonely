"""PII access audit logging — records who accessed patient data, not the data itself."""

from __future__ import annotations

import logging

logger = logging.getLogger("fonely.pii_audit")


def log_pii_access(
    operation: str,
    data_type: str,
    business_id: int,
    accessor: str,
    record_count: int,
    correlation_id: str | None = None,
) -> None:
    logger.info(
        "pii_access",
        extra={
            "operation": operation,
            "data_type": data_type,
            "business_id": business_id,
            "accessor": accessor,
            "record_count": record_count,
            "correlation_id": correlation_id,
        },
    )
