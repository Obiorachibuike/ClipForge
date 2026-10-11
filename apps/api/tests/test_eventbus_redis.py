"""Redis event fan-out uses the same namespaced channel the bridge subscribes to."""
from __future__ import annotations

from app.services.events import REDIS_CHANNEL_PREFIX, Event, EventBus


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))


def test_event_publish_uses_bridge_prefix_and_deduplicates_redis_echo(monkeypatch):
    from app.services import events

    redis = _FakeRedis()
    monkeypatch.setattr(events, "get_redis", lambda: redis)
    bus = EventBus()
    event = Event(type="job.progress", channel="job:123", payload={"progress": 10}, event_id="evt-1")

    bus.publish(event)
    # The bridge receives this message from the same Redis instance. It must not
    # deliver the publisher's local event a second time.
    bus._dispatch(event, relay_to_redis=False)

    assert redis.published == [(f"{REDIS_CHANNEL_PREFIX}job:123", event.to_json())]
    assert [item["event_id"] for item in bus.recent()] == ["evt-1"]


def test_subscriber_receives_a_multi_channel_event_only_once():
    import asyncio

    from app.services.events import _Subscriber

    loop = asyncio.new_event_loop()
    try:
        sub = _Subscriber({"user:1", "project:2"}, loop)
        first = Event(type="job.progress", channel="project:2", event_id="same-event")
        second = Event(type="job.progress", channel="user:1", event_id="same-event")
        assert sub.should_deliver(first)
        assert not sub.should_deliver(second)
    finally:
        loop.close()
