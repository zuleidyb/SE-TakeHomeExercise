"""
Targeted implementation slice for the Product Template Update Tracking design.

Scope: the EngagementTemplateState index — the piece that lets us answer
"which engagements are behind, and by how much" without ever loading an
engagement file. This module implements the two event handlers (engagement
lifecycle, template publish) and the two read paths (pending-updates list,
per-engagement pending-changes detail), against a storage Protocol so the
same service logic works whether the backing store is DynamoDB or, as here,
an in-memory fake for testing.

Deliberately out of scope: the actual HTTP layer, the LLM call inside the
diff summarizer (stubbed as a store lookup), and the engagement
apply/decline logic itself (owned by the existing engagement system per the
assignment).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol


class LifecycleAction(str, Enum):
    CREATE = "create"
    APPLY = "apply"
    DECLINE = "decline"


@dataclass(frozen=True)
class EngagementLifecycleEvent:
    """Emitted by the engagement system's outbox on create/apply/decline.

    `sequence` is a monotonically increasing counter per engagement, assigned
    by the engagement system at write time. It's what lets us detect
    duplicate and out-of-order delivery, which an at-least-once event bus
    guarantees we'll eventually see.
    """

    engagement_id: str
    firm_id: str
    template_id: str
    version: int
    action: LifecycleAction
    sequence: int


@dataclass(frozen=True)
class TemplatePublishedEvent:
    """Emitted by the template store's publish hook."""

    template_id: str
    version: int


@dataclass
class EngagementState:
    engagement_id: str
    firm_id: str
    template_id: str
    current_version: int
    latest_available_version: int
    pending_update: bool
    last_event_sequence: int


class EngagementStateStore(Protocol):
    def get(self, engagement_id: str) -> Optional[EngagementState]: ...
    def upsert(self, state: EngagementState) -> None: ...
    def query_by_template(self, template_id: str) -> list[EngagementState]: ...
    def query_by_firm(self, firm_id: str) -> list[EngagementState]: ...


class TemplateVersionStore(Protocol):
    def get_latest_version(self, template_id: str) -> Optional[int]: ...
    def set_latest_version(self, template_id: str, version: int) -> None: ...
    def get_diff_summary(self, template_id: str, version: int) -> Optional[str]:
        """Human-readable summary of the diff introduced BY `version`
        (i.e. the change from version-1 to version)."""
        ...


class InMemoryEngagementStateStore:
    """Fake store for tests / local dev. A real implementation would be a
    DynamoDB table with a GSI on template_id and another on firm_id."""

    def __init__(self) -> None:
        self._rows: dict[str, EngagementState] = {}

    def get(self, engagement_id: str) -> Optional[EngagementState]:
        return self._rows.get(engagement_id)

    def upsert(self, state: EngagementState) -> None:
        self._rows[state.engagement_id] = state

    def query_by_template(self, template_id: str) -> list[EngagementState]:
        return [s for s in self._rows.values() if s.template_id == template_id]

    def query_by_firm(self, firm_id: str) -> list[EngagementState]:
        return [s for s in self._rows.values() if s.firm_id == firm_id]


class InMemoryTemplateVersionStore:
    def __init__(self) -> None:
        self._latest: dict[str, int] = {}
        self._summaries: dict[tuple[str, int], str] = {}

    def get_latest_version(self, template_id: str) -> Optional[int]:
        return self._latest.get(template_id)

    def set_latest_version(self, template_id: str, version: int) -> None:
        self._latest[template_id] = version

    def get_diff_summary(self, template_id: str, version: int) -> Optional[str]:
        return self._summaries.get((template_id, version))

    def put_diff_summary(self, template_id: str, version: int, summary: str) -> None:
        self._summaries[(template_id, version)] = summary


class TrackingService:
    """Owns the two event handlers and two read paths described in the
    design doc's High-Level Architecture section."""

    def __init__(
        self,
        state_store: EngagementStateStore,
        version_store: TemplateVersionStore,
    ) -> None:
        self._states = state_store
        self._versions = version_store

    # -- writes -----------------------------------------------------------

    def handle_lifecycle_event(
        self, event: EngagementLifecycleEvent
    ) -> EngagementState:
        """Idempotent, out-of-order-safe upsert into the index.

        Contract: calling this twice with the same event, or with events
        delivered out of sequence order, converges to the same final state
        as calling it once in sequence order.
        """
        existing = self._states.get(event.engagement_id)
        if existing is not None and event.sequence <= existing.last_event_sequence:
            # Duplicate delivery, or a stale/out-of-order event arriving
            # after a newer one already landed. Ignore rather than regress
            # the state.
            return existing

        latest_version = self._versions.get_latest_version(event.template_id)
        if latest_version is None or event.version > latest_version:
            # First time we've seen this template, or this engagement was
            # created/updated to a version newer than what we'd recorded as
            # latest (e.g. index bootstrap ordering). Treat the engagement's
            # own version as the floor for "latest known".
            latest_version = event.version
            self._versions.set_latest_version(event.template_id, latest_version)

        new_state = EngagementState(
            engagement_id=event.engagement_id,
            firm_id=event.firm_id,
            template_id=event.template_id,
            current_version=event.version,
            latest_available_version=latest_version,
            pending_update=latest_version > event.version,
            last_event_sequence=event.sequence,
        )
        self._states.upsert(new_state)
        return new_state

    def handle_template_published(
        self, event: TemplatePublishedEvent
    ) -> list[EngagementState]:
        """Fan out a publish to every engagement on this template that's
        now behind. Idempotent: a duplicate or stale (version <= known
        latest) publish event is a no-op.

        PRODUCTION NOTE: this in-memory version loops through every match
        synchronously. See design doc, Failure Modes & Tradeoffs -- "Fan-out
        cost on widely-used templates": at real scale, the GSI query on
        template_id would be paginated, and writes dispatched as batched SQS
        messages across a pool of Lambda workers, since a template shared by
        tens of thousands of engagements would blow a single invocation's
        time/memory limits if run as one big loop.

        Returns the list of engagements newly flagged, for observability
        (e.g. counting fan-out size per publish).
        """
        current_latest = self._versions.get_latest_version(event.template_id)
        if current_latest is not None and event.version <= current_latest:
            return []

        self._versions.set_latest_version(event.template_id, event.version)

        newly_flagged: list[EngagementState] = []
        for state in self._states.query_by_template(event.template_id):
            if state.current_version < event.version:
                state.latest_available_version = event.version
                state.pending_update = True
                self._states.upsert(state)
                newly_flagged.append(state)
        return newly_flagged

    # -- reads --------------------------------------------------------------

    def get_pending_updates_for_firm(self, firm_id: str) -> list[EngagementState]:
        """Backs GET /firms/{id}/pending-updates. Single index query, no
        engagement loads, no cross-firm scan."""
        return [s for s in self._states.query_by_firm(firm_id) if s.pending_update]

    def get_pending_changes(self, engagement_id: str) -> list[str]:
        """Backs GET /engagements/{id}/pending-changes. Returns ordered,
        precomputed per-hop summaries rather than one merged diff, so the
        user (and the audit trail) can see what changed in each release."""
        state = self._states.get(engagement_id)
        if state is None or not state.pending_update:
            return []

        summaries = []
        for version in range(state.current_version + 1, state.latest_available_version + 1):
            summary = self._versions.get_diff_summary(state.template_id, version)
            # A missing summary (e.g. the LLM summarizer hasn't finished, or
            # failed) degrades to a placeholder rather than silently
            # dropping the hop -- traceability matters more than tidiness
            # here.
            summaries.append(summary or f"[summary unavailable for v{version}]")
        return summaries
