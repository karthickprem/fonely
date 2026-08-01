"""Injected validation boundary for authoritative appointment proposals."""

from typing import Protocol

from fonely.domain.pending_actions.commands import ActorContext
from fonely.domain.pending_actions.payloads import PendingAppointmentEnvelope


class AppointmentValidationPort(Protocol):
    """Resolve authoritative facts and assert appointment-specific authorization.

    Implementations own all tenant-scoped reads. Returning successfully asserts that
    every referenced object belongs to the business and, for actor operations, that
    the actor may create or mutate the target appointment. Returned create facts,
    cancellation current facts, and reschedule old/new facts must contain the owning
    business's authoritative timezone; a submitted timezone must be replaced or the
    payload rejected when it differs from that authoritative value.

    Validation before a write is authoritative only when its reads and the eventual
    appointment/allocation mutation share one transaction with locking sufficient to
    keep every validated revision stable. Otherwise the engine must establish an
    equivalent authoritative revision-stability guarantee. In either case,
    ``validate_stored`` must be called again by ``begin_commit`` and must re-read and
    revalidate mutable appointment, service, resource, schedule, eligibility, tenant,
    and version facts. Reschedules that have become no-ops must be rejected. This port
    defines that D2 contract; it does not implement the D3 transaction or locking.
    Confirmation snapshots and payload digests remain the authority for detecting any
    change between confirmed and revalidated facts.
    """

    async def validate_for_actor(
        self,
        actor: ActorContext,
        payload: PendingAppointmentEnvelope,
    ) -> PendingAppointmentEnvelope: ...

    async def validate_stored(
        self,
        business_id: int,
        payload: PendingAppointmentEnvelope,
    ) -> PendingAppointmentEnvelope: ...

    async def validate_idempotent_retry(
        self,
        actor: ActorContext,
        proposed: PendingAppointmentEnvelope,
        stored: PendingAppointmentEnvelope,
    ) -> None:
        """Assert authorization and semantic equivalence without mutable re-resolution."""
        ...

    async def validate_completion_evidence(
        self,
        business_id: int,
        payload: PendingAppointmentEnvelope,
        committed_entity_type: str,
        committed_entity_id: int,
    ) -> None:
        """Assert post-mutation evidence matches the operation, target, and facts."""
        ...
