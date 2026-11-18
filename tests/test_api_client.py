"""Unit tests for the API client using httpx's in-memory MockTransport."""
import asyncio
import json

import httpx
import pytest

from app.api_client import ApiError, ConfigServiceClient

PART = {"uuid": "p1", "partNo": "TM-1020", "name": "Power Cell", "status": "active", "unit": "pcs"}


def make_client(handler):
    return ConfigServiceClient("https://example.test/prod", "secret",
                               transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def test_sends_api_key_and_parses_roots():
    seen = {}

    def handler(request):
        seen["key"] = request.headers["x-api-key"]
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json=[
            {"uuid": "c1", "partUuid": "p0", "quantity": 1, "endUnitSerialNo": "00001"}])

    roots = run(make_client(handler).get_children())
    assert seen == {"key": "secret", "query": {}}
    assert roots[0].end_unit_serial_no == "00001" and roots[0].is_end_unit


def test_children_passes_parent_uuid():
    def handler(request):
        assert request.url.params["parentUuid"] == "c1"
        return httpx.Response(200, json=[{"uuid": "c2", "partUuid": "p1", "quantity": 4}])

    [child] = run(make_client(handler).get_children("c1"))
    assert child.quantity == 4 and not child.is_end_unit


def test_part_is_cached():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=PART)

    async def scenario():
        client = make_client(handler)
        await client.get_part("p1")
        await client.get_part("p1")
        await client.get_part("p1", refresh=True)

    run(scenario())
    assert len(calls) == 2


def test_update_sends_only_given_fields_and_refreshes_cache():
    def handler(request):
        assert request.method == "PUT"
        assert json.loads(request.content) == {"status": "deprecated"}
        return httpx.Response(200, json={**PART, "status": "deprecated"})

    part = run(make_client(handler).update_part("p1", status="deprecated"))
    assert part.status == "deprecated"


def test_invalid_transition_exposes_allowable_statuses():
    def handler(request):
        return httpx.Response(400, json={"error": "Invalid status change",
                                         "allowableStatuses": ["active"]})

    with pytest.raises(ApiError) as exc:
        run(make_client(handler).update_part("p1", status="recalled"))
    assert exc.value.status_code == 400
    assert exc.value.allowable_statuses == ["active"]
    assert "Invalid status change" in str(exc.value)


@pytest.mark.parametrize("body, expected", [
    ({"error": "Part not found"}, "Part not found"),
    ({"message": "Forbidden"}, "Forbidden"),
    ({"success": False, "error": {"name": "ZodError", "message": "[...]"}}, "rejected as invalid"),
])
def test_error_shapes_are_normalised(body, expected):
    with pytest.raises(ApiError) as exc:
        run(make_client(lambda r: httpx.Response(404, json=body)).get_part("x"))
    assert expected in exc.value.message


def test_network_failure_becomes_api_error():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(ApiError) as exc:
        run(make_client(handler).get_children())
    assert exc.value.status_code is None


def test_allowable_transitions_exclude_current():
    def handler(request):
        return httpx.Response(200, json={"partUuid": "p1", "currentStatus": "draft",
                                         "allowableStatuses": ["draft", "experimental"]})

    result = run(make_client(handler).get_allowable_statuses("p1"))
    assert result.transitions == ["experimental"]


def test_calls_are_logged_without_the_api_key(caplog):
    def handler(request):
        if request.method == "PUT":
            return httpx.Response(400, json={"error": "Invalid status change", "allowableStatuses": []})
        return httpx.Response(200, json=[])

    async def scenario():
        client = make_client(handler)
        await client.get_children("c1")
        with pytest.raises(ApiError):
            await client.update_part("p1", status="recalled")

    with caplog.at_level("INFO", logger="temporal.api"):
        run(scenario())
    lines = [r.getMessage() for r in caplog.records]
    assert lines[0].startswith("GET /api/configs/children?parentUuid=c1 -> 200")
    assert lines[1].startswith('PUT /api/parts/p1 body={"status": "recalled"} -> 400 Invalid status change')
    assert caplog.records[1].levelname == "WARNING"
    assert all("secret" not in line for line in lines)
