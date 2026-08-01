"""ORM boundaries for tenant-scoped appointment entities."""

import warnings

from sqlalchemy import and_, select
from sqlalchemy.exc import SAWarning
from sqlalchemy.orm import configure_mappers

from fonely.models.schema import (
    Appointment,
    AppointmentCommit,
    Base,
    OperatingSchedule,
    Resource,
    ResourceAllocation,
    ScheduleException,
    ServiceResourceEligibility,
)


def test_mapper_configuration_has_no_overlap_warnings() -> None:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", SAWarning)
        configure_mappers()
    assert not [item for item in captured if issubclass(item.category, SAWarning)]


def test_secondary_tenant_relationships_are_not_mapped() -> None:
    absent_relationships = {
        OperatingSchedule: ("resource",),
        ScheduleException: ("resource",),
        ServiceResourceEligibility: ("service", "resource"),
        Appointment: ("service", "resource", "allocations", "commits"),
        ResourceAllocation: ("appointment", "resource"),
        AppointmentCommit: ("appointment",),
    }
    for model, names in absent_relationships.items():
        mapped = model.__mapper__.relationships.keys()
        for name in names:
            assert name not in mapped


def test_primary_business_relationship_remains_mapped() -> None:
    assert "business" in Appointment.__mapper__.relationships
    assert "appointments" in Appointment.__mapper__.relationships["business"].back_populates


def test_explicit_tenant_join_uses_business_and_entity_ids() -> None:
    statement = select(ResourceAllocation).join(
        Resource,
        and_(
            Resource.business_id == ResourceAllocation.business_id,
            Resource.id == ResourceAllocation.resource_id,
        ),
    )
    compiled = str(statement)
    assert "resources.business_id = resource_allocations.business_id" in compiled
    assert "resources.id = resource_allocations.resource_id" in compiled


def test_scalar_ids_are_the_d3_write_boundary() -> None:
    allocation_columns = set(Base.metadata.tables["resource_allocations"].columns.keys())
    assert {"business_id", "resource_id", "appointment_id", "pending_action_id"} <= (
        allocation_columns
    )
    assert not ResourceAllocation.__mapper__.relationships.keys()
