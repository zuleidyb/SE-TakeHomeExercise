import pytest

from tracking_service import (
    EngagementLifecycleEvent,
    InMemoryEngagementStateStore,
    InMemoryTemplateVersionStore,
    LifecycleAction,
    TemplatePublishedEvent,
    TrackingService,
)


@pytest.fixture
def service() -> TrackingService:
    return TrackingService(InMemoryEngagementStateStore(), InMemoryTemplateVersionStore())


def create_event(engagement_id="eng-1", firm_id="firm-1", template_id="tmpl-1", version=1, sequence=1):
    return EngagementLifecycleEvent(
        engagement_id=engagement_id,
        firm_id=firm_id,
        template_id=template_id,
        version=version,
        action=LifecycleAction.CREATE,
        sequence=sequence,
    )


def test_create_populates_index_not_pending(service):
    state = service.handle_lifecycle_event(create_event())
    assert state.current_version == 1
    assert state.pending_update is False


def test_publish_flags_behind_engagements(service):
    service.handle_lifecycle_event(create_event(version=1, sequence=1))
    affected = service.handle_template_published(TemplatePublishedEvent("tmpl-1", 2))

    assert len(affected) == 1
    assert affected[0].pending_update is True
    assert affected[0].latest_available_version == 2

    pending = service.get_pending_updates_for_firm("firm-1")
    assert [s.engagement_id for s in pending] == ["eng-1"]


def test_publish_does_not_flag_engagements_already_current(service):
    service.handle_lifecycle_event(create_event(version=2, sequence=1))
    affected = service.handle_template_published(TemplatePublishedEvent("tmpl-1", 2))
    assert affected == []
    assert service.get_pending_updates_for_firm("firm-1") == []


def test_duplicate_lifecycle_event_is_idempotent(service):
    first = service.handle_lifecycle_event(create_event(sequence=1))
    second = service.handle_lifecycle_event(create_event(sequence=1))
    assert first == second


def test_out_of_order_lifecycle_event_is_ignored(service):
    # sequence 2 (apply) lands before sequence 1 (create) is retried/redelivered
    apply_event = EngagementLifecycleEvent(
        "eng-1", "firm-1", "tmpl-1", version=2, action=LifecycleAction.APPLY, sequence=2
    )
    service.handle_lifecycle_event(apply_event)

    stale_create = create_event(version=1, sequence=1)
    result = service.handle_lifecycle_event(stale_create)

    # Must NOT regress current_version back to 1
    assert result.current_version == 2


def test_duplicate_publish_event_is_idempotent(service):
    service.handle_lifecycle_event(create_event(version=1, sequence=1))
    first = service.handle_template_published(TemplatePublishedEvent("tmpl-1", 2))
    second = service.handle_template_published(TemplatePublishedEvent("tmpl-1", 2))

    assert len(first) == 1
    assert second == []  # already-applied publish is a no-op, not re-flagged/re-counted


def test_apply_clears_pending_flag(service):
    service.handle_lifecycle_event(create_event(version=1, sequence=1))
    service.handle_template_published(TemplatePublishedEvent("tmpl-1", 2))

    apply_event = EngagementLifecycleEvent(
        "eng-1", "firm-1", "tmpl-1", version=2, action=LifecycleAction.APPLY, sequence=2
    )
    state = service.handle_lifecycle_event(apply_event)

    assert state.pending_update is False
    assert service.get_pending_updates_for_firm("firm-1") == []


def test_multi_hop_accumulation_returns_ordered_summaries(service):
    service.handle_lifecycle_event(create_event(version=1, sequence=1))

    versions = service._versions  # test-only reach-in to seed summaries
    versions.put_diff_summary("tmpl-1", 2, "v2: added risk-rating field")
    versions.put_diff_summary("tmpl-1", 3, "v3: renamed 'sign-off' to 'approval'")

    service.handle_template_published(TemplatePublishedEvent("tmpl-1", 2))
    service.handle_template_published(TemplatePublishedEvent("tmpl-1", 3))

    changes = service.get_pending_changes("eng-1")
    assert changes == [
        "v2: added risk-rating field",
        "v3: renamed 'sign-off' to 'approval'",
    ]


def test_missing_summary_degrades_to_placeholder_not_dropped(service):
    service.handle_lifecycle_event(create_event(version=1, sequence=1))
    service.handle_template_published(TemplatePublishedEvent("tmpl-1", 2))

    changes = service.get_pending_changes("eng-1")
    assert changes == ["[summary unavailable for v2]"]


def test_publish_fan_out_only_affects_matching_template(service):
    service.handle_lifecycle_event(create_event(engagement_id="eng-1", template_id="tmpl-1", version=1, sequence=1))
    service.handle_lifecycle_event(create_event(engagement_id="eng-2", template_id="tmpl-2", version=1, sequence=1))

    affected = service.handle_template_published(TemplatePublishedEvent("tmpl-1", 2))
    assert [s.engagement_id for s in affected] == ["eng-1"]
