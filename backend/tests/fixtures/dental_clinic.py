"""Synthetic dental clinic configuration for onboarding integration tests."""

from typing import Any


def _prov(path: str) -> dict[str, Any]:
    return {
        "review_status": "clear",
        "sources": [{"source_type": "operator_entry", "source_id": "test-setup"}],
    }


def _field_provenance(*paths: str) -> dict[str, Any]:
    return {"fields": tuple((p, _prov(p)) for p in paths)}


DENTAL_CLINIC_DRAFT: dict[str, Any] = {
    "schema_version": 1,
    "draft_id": "smile-dental-clinic",
    "business_name": "Smile Dental Clinic",
    "business_category": "clinic",
    "default_timezone": "Asia/Kolkata",
    "default_currency": "INR",
    "preferred_languages": ["ta-IN", "en-IN"],
    "contact": {"phone": "+914428350001"},
    "policy": {
        "advance_booking_days": 30,
        "minimum_notice_minutes": 60,
        "owner_review_required": True,
        "provenance": _field_provenance("advance_booking_days", "minimum_notice_minutes"),
    },
    "locations": [
        {
            "key": "aminjikarai",
            "display_name": "Aminjikarai Branch",
            "address": {
                "line1": "42 Nelson Manickam Road",
                "city": "Chennai",
                "state": "Tamil Nadu",
                "pincode": "600029",
            },
            "contact": {},
            "schedule": {
                "periods": [{"day": d, "start": "10:00", "end": "13:00"} for d in range(6)]
                + [{"day": d, "start": "17:00", "end": "20:30"} for d in range(6)],
                "exceptions": [
                    {"date": "2026-08-15", "is_closed": True, "reason": "Independence Day"},
                ],
            },
            "provenance": _field_provenance(
                "display_name",
                "is_active",
                "schedule",
                "address.line1",
                "address.city",
                "address.state",
                "address.pincode",
            ),
        }
    ],
    "services": [
        {
            "key": "general-consultation",
            "name": "General Consultation",
            "duration_minutes": 20,
            "buffer_before_minutes": 0,
            "buffer_after_minutes": 10,
            "price": {
                "kind": "fixed",
                "currency": "INR",
                "amount": "300",
                "provenance": _prov("price"),
            },
            "eligible_resource_keys": ["dr-priya", "dr-arjun"],
            "location_keys": ["aminjikarai"],
            "requires_resource": True,
            "is_active": True,
            "provenance": _field_provenance(
                "name",
                "duration_minutes",
                "is_active",
                "price",
                "eligible_resource_keys",
                "requires_resource",
                "location_keys",
                "buffer_after_minutes",
            ),
        },
        {
            "key": "root-canal",
            "name": "Root Canal",
            "duration_minutes": 60,
            "buffer_before_minutes": 0,
            "buffer_after_minutes": 15,
            "price": {
                "kind": "range",
                "currency": "INR",
                "minimum": "3500",
                "maximum": "5500",
                "provenance": _prov("price"),
            },
            "eligible_resource_keys": ["dr-priya"],
            "location_keys": ["aminjikarai"],
            "requires_resource": True,
            "is_active": True,
            "provenance": _field_provenance(
                "name",
                "duration_minutes",
                "is_active",
                "price",
                "eligible_resource_keys",
                "requires_resource",
                "location_keys",
                "buffer_after_minutes",
            ),
        },
        {
            "key": "scaling",
            "name": "Scaling & Polishing",
            "duration_minutes": 30,
            "buffer_before_minutes": 0,
            "buffer_after_minutes": 10,
            "price": {
                "kind": "fixed",
                "currency": "INR",
                "amount": "800",
                "provenance": _prov("price"),
            },
            "eligible_resource_keys": ["dr-priya"],
            "location_keys": ["aminjikarai"],
            "requires_resource": True,
            "is_active": True,
            "provenance": _field_provenance(
                "name",
                "duration_minutes",
                "is_active",
                "price",
                "eligible_resource_keys",
                "requires_resource",
                "location_keys",
                "buffer_after_minutes",
            ),
        },
        {
            "key": "extraction",
            "name": "Tooth Extraction",
            "duration_minutes": 30,
            "buffer_before_minutes": 0,
            "buffer_after_minutes": 15,
            "price": {
                "kind": "range",
                "currency": "INR",
                "minimum": "500",
                "maximum": "1500",
                "provenance": _prov("price"),
            },
            "eligible_resource_keys": ["dr-priya"],
            "location_keys": ["aminjikarai"],
            "requires_resource": True,
            "is_active": True,
            "provenance": _field_provenance(
                "name",
                "duration_minutes",
                "is_active",
                "price",
                "eligible_resource_keys",
                "requires_resource",
                "location_keys",
                "buffer_after_minutes",
            ),
        },
        {
            "key": "orthodontic-consultation",
            "name": "Orthodontic Consultation",
            "duration_minutes": 30,
            "buffer_before_minutes": 0,
            "buffer_after_minutes": 10,
            "price": {
                "kind": "fixed",
                "currency": "INR",
                "amount": "500",
                "provenance": _prov("price"),
            },
            "eligible_resource_keys": ["dr-arjun"],
            "location_keys": ["aminjikarai"],
            "requires_resource": True,
            "is_active": True,
            "provenance": _field_provenance(
                "name",
                "duration_minutes",
                "is_active",
                "price",
                "eligible_resource_keys",
                "requires_resource",
                "location_keys",
                "buffer_after_minutes",
            ),
        },
    ],
    "resources": [
        {
            "key": "dr-priya",
            "display_name": "Dr. Priya Krishnan",
            "resource_type": "staff",
            "location_keys": ["aminjikarai"],
            "service_keys": [
                "general-consultation",
                "root-canal",
                "scaling",
                "extraction",
            ],
            "schedule": {
                "periods": [{"day": d, "start": "10:00", "end": "13:00"} for d in range(6)]
                + [{"day": d, "start": "17:00", "end": "20:30"} for d in range(6)],
                "exceptions": [
                    {"date": "2026-08-15", "is_closed": True, "reason": "Independence Day"},
                ],
            },
            "is_active": True,
            "provenance": _field_provenance(
                "display_name",
                "resource_type",
                "is_active",
                "location_keys",
                "service_keys",
                "schedule",
            ),
        },
        {
            "key": "dr-arjun",
            "display_name": "Dr. Arjun Venkatesh",
            "resource_type": "staff",
            "location_keys": ["aminjikarai"],
            "service_keys": [
                "orthodontic-consultation",
                "general-consultation",
            ],
            "schedule": {
                "periods": [{"day": d, "start": "10:00", "end": "13:00"} for d in range(6)]
                + [{"day": d, "start": "17:00", "end": "20:30"} for d in range(6)],
                "exceptions": [
                    {"date": "2026-08-15", "is_closed": True, "reason": "Independence Day"},
                ],
            },
            "is_active": True,
            "provenance": _field_provenance(
                "display_name",
                "resource_type",
                "is_active",
                "location_keys",
                "service_keys",
                "schedule",
            ),
        },
    ],
    "provenance": _field_provenance(
        "business_name",
        "business_category",
        "default_timezone",
        "default_currency",
        "contact.phone",
    ),
}
