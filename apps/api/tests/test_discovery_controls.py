"""Clip-discovery controls are validated, bounded and honest about shortfall."""
from __future__ import annotations

import pytest
from app.schemas.media import DiscoverClipsRequest
from app.services.pipeline.discover_job import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MAX_DURATION,
    DEFAULT_MIN_DURATION,
    DURATION_CEILING,
    DURATION_FLOOR,
    MAX_CANDIDATES_LIMIT,
    discovery_options,
)
from pydantic import ValidationError


def test_defaults_match_the_documented_product_limits():
    request = DiscoverClipsRequest()
    assert request.max_candidates == DEFAULT_MAX_CANDIDATES
    assert (request.min_duration, request.max_duration) == (DEFAULT_MIN_DURATION, DEFAULT_MAX_DURATION)


@pytest.mark.parametrize(
    "payload",
    [
        {"max_candidates": 0},
        {"max_candidates": MAX_CANDIDATES_LIMIT + 1},
        {"max_candidates": 10_000},
        {"min_duration": 0},
        {"min_duration": DURATION_CEILING + 1},
        {"max_duration": 10_000},
        {"min_duration": 60, "max_duration": 30},
    ],
)
def test_invalid_discovery_controls_are_rejected(payload):
    with pytest.raises(ValidationError):
        DiscoverClipsRequest(**payload)


def test_discovery_request_rejects_keys_the_server_owns():
    """A client must not be able to inject project/video identifiers."""
    with pytest.raises(ValidationError):
        DiscoverClipsRequest(video_id="attacker-chosen")


def test_equal_duration_bounds_are_allowed():
    request = DiscoverClipsRequest(min_duration=30, max_duration=30)
    assert (request.min_duration, request.max_duration) == (30.0, 30.0)


def test_api_rejects_invalid_discovery_controls_before_queueing_a_job(auth_api):
    project_id = auth_api.post("/api/v1/projects", json={"name": "Controls"}).json()["id"]
    response = auth_api.post(
        f"/api/v1/projects/{project_id}/discover",
        json={"max_candidates": 10_000, "min_duration": 5, "max_duration": 60},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_worker_reclamps_a_hand_written_payload():
    assert discovery_options(
        {"max_candidates": 10_000, "min_duration": -30, "max_duration": 10_000}
    ) == (MAX_CANDIDATES_LIMIT, DURATION_FLOOR, DURATION_CEILING)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"max_candidates": "not-a-number"},
        {"min_duration": None},
        {"max_duration": float("nan")},
        {"max_duration": float("inf")},
    ],
)
def test_malformed_payloads_fall_back_to_defaults(payload):
    assert discovery_options(payload) == (
        DEFAULT_MAX_CANDIDATES,
        DEFAULT_MIN_DURATION,
        DEFAULT_MAX_DURATION,
    )


def test_reversed_duration_bounds_are_clamped_in_the_worker():
    """A durable payload written by an older caller must never raise in the job."""
    assert discovery_options({"min_duration": 120, "max_duration": 30}) == (
        DEFAULT_MAX_CANDIDATES,
        120.0,
        120.0,
    )
