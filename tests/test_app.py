import pytest

from app import app, engine


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    engine.reset()
    return app.test_client()


def tx(tx_id, source, target, at="2026-06-08T12:00:00Z", **extra):
    return {"txId": tx_id, "fromUserId": source, "toUserId": target, "amount": 100, "createdAt": at, **extra}


def scores(client, items):
    response = client.post("/ghost-chains/transactions", json={"transactions": items})
    assert response.status_code == 200
    return [item["riskScore"] for item in response.json["transactions"]]


def test_health_and_reset(client):
    assert client.get("/ghost-chains/health").json == {"status": "ok"}
    assert client.post("/ghost-chains/reset", json={"clearTransactions": True}).json == {"clearTransactions": True}


def test_structural_ordering(client):
    isolated = scores(client, [tx("a", "meridian", "apex")])[0]
    client.post("/ghost-chains/reset", json={"clearTransactions": True})
    all_scores = scores(client, [
        tx("a", "meridian", "apex"), tx("b", "apex", "cascade"),
        tx("c", "cascade", "meridian"), tx("d", "apex", "nimbus"),
        tx("e", "nimbus", "meridian"),
    ])
    extension, return_score, multi = all_scores[1], all_scores[2], all_scores[4]
    assert isolated < extension < return_score < multi


def test_identity_shift_and_disconnected_reuse(client):
    consistent = scores(client, [tx("a", "m", "a", deviceId="dev-1"), tx("b", "a", "c", deviceId="dev-1")])[-1]
    client.post("/ghost-chains/reset", json={"clearTransactions": True})
    shifted = scores(client, [tx("a", "m", "a", deviceId="dev-1"), tx("b", "a", "c", deviceId="dev-2")])[-1]
    assert shifted > consistent
    reused = scores(client, [tx("c", "x", "y", ipAddress="10.0.0.1"), tx("d", "p", "q", ipAddress="10.0.0.1")])[-1]
    assert reused > 0.02


def test_idempotency_and_order(client):
    items = [tx("a", "m", "a"), tx("b", "a", "c")]
    first = scores(client, items)
    second = scores(client, items)
    assert first == second
    assert client.post("/ghost-chains/transactions", json={"transactions": [tx("a", "different", "a")]}).status_code == 400


def test_missing_identity_on_connected_path_is_observable(client):
    first, missing = scores(client, [tx("a", "m", "a", deviceId="dev-1"), tx("b", "a", "c")])
    assert missing > first


def test_unknown_fields_and_score_bounds(client):
    result = client.post("/ghost-chains/transactions", json={"transactions": [tx("a", "m", "a", futureField=True)]})
    assert result.status_code == 200
    assert 0.0 <= result.json["transactions"][0]["riskScore"] <= 1.0


def test_lookback_expiration_uses_created_at_not_arrival_order(client):
    scores(client, [tx("old", "m", "a", "2026-06-07T12:00:00Z"), tx("new", "a", "c", "2026-06-08T12:00:00Z")])
    # The old record is exactly 24 hours before the new record and remains active.
    assert scores(client, [tx("boundary", "c", "m", "2026-06-08T12:00:00Z")])[0] > 0.02
    # A later timestamp expires the boundary record and the old graph disappears.
    assert scores(client, [tx("later", "x", "y", "2026-06-09T12:00:01Z")])[0] == 0.02
