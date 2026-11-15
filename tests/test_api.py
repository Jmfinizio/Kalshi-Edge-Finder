"""HTTP-level tests: the same endpoints the UI calls, driven through FastAPI's TestClient on demo data."""
import pytest
from fastapi.testclient import TestClient

from kalshi_edge.core import Settings, kalshi_fee
from kalshi_edge.demo import DemoKalshi, DemoOdds, DemoWorld
from kalshi_edge.server import create_app
from kalshi_edge.service import Engine
from kalshi_edge.sports import SPORTS
from kalshi_edge.store import Store


@pytest.fixture()
def client():
    sl = [SPORTS["nfl"]]
    w = DemoWorld(sl, seed=3)
    e = Engine(Settings(), DemoOdds(w), DemoKalshi(w), Store(":memory:"), sl, demo=True, demo_world=w)
    with TestClient(create_app(engine=e, run_loop=False)) as c:
        assert c.post("/api/refresh", json={"source": "both"}).status_code == 200
        yield c


def test_status_board_and_opportunities(client):
    s = client.get("/api/status").json()
    assert s["mode"] == "demo" and s["matched"] == 12 and s["games_from_books"] == 6 and s["errors"] == []
    board = client.get("/api/board").json()["rows"]
    assert len(board) == 12 and all("gap" in r and "flags" in r for r in board)
    opps = client.get("/api/opportunities").json()["rows"]
    assert opps and [o["net_edge"] for o in opps] == sorted((o["net_edge"] for o in opps), reverse=True)
    assert client.get("/api/opportunities", params={"min_edge": 0.5}).json()["rows"] == []
    d = client.get("/api/diagnostics").json()
    assert d["unmatched_kalshi"] == [] and d["unmatched_books"] == []


def test_index_serves_ui_without_secrets(client):
    r = client.get("/")
    assert r.status_code == 200 and "Kalshi vs Sportsbook" in r.text and "apiKey" not in r.text
    assert "ODDS_API_KEY" not in str(client.get("/api/status").json())


def test_paper_trade_validation_and_settlement(client):
    top = client.get("/api/opportunities").json()["rows"][0]
    post = lambda **kw: client.post("/api/paper-trades", json={"ticker": top["ticker"], "side": top["best_side"], "qty": 20, **kw})
    assert post(side="MAYBE").status_code == 400
    assert client.post("/api/paper-trades", json={"ticker": "NOPE", "side": "YES", "qty": 5}).status_code == 400
    assert post(qty=0).status_code == 422
    ok = post().json()
    assert ok["qty"] == 20 and ok["fee"] == kalshi_fee(ok["price"], 20)
    assert client.post("/api/settle").json() == {"settled": 0}
    assert client.post("/api/demo/finish-games").json()["settled"] == 1
    perf = client.get("/api/performance").json()
    assert perf["settled"] == 1 and perf["open"] == 0 and perf["wins"] + perf["losses"] == 1
    tr = client.get("/api/paper-trades").json()["rows"][0]
    assert tr["status"] == "settled" and tr["pnl"] is not None
    client.delete(f"/api/paper-trades/{tr['id']}")
    assert client.get("/api/paper-trades").json()["rows"] == []


def test_settings_roundtrip_changes_results(client):
    n0 = client.get("/api/status").json()["opportunities"]
    s = client.post("/api/settings", json={"min_net_edge": 0.05, "devig_method": "bogus", "nonsense": 1}).json()
    assert s["min_net_edge"] == 0.05 and s["devig_method"] == "power" and "nonsense" not in s
    assert client.get("/api/status").json()["opportunities"] < n0


def test_history_and_demo_only_route(client):
    top = client.get("/api/opportunities").json()["rows"][0]
    assert "rows" in client.get(f"/api/history/{top['ticker']}").json()
    assert client.post("/api/refresh", json={"source": "book"}).status_code == 200


def test_demo_route_hidden_in_live_mode():
    w = DemoWorld()
    e = Engine(Settings(), None, DemoKalshi(w), Store(":memory:"), [SPORTS["nfl"]], demo=False)
    with TestClient(create_app(engine=e, run_loop=False)) as c:
        assert c.post("/api/demo/finish-games").status_code == 404
        assert c.post("/api/refresh", json={"source": "book"}).status_code == 400       # no key configured


def test_api_key_never_logged(caplog):
    """httpx would otherwise log '...odds?apiKey=SECRET...' at INFO level."""
    import asyncio, logging, httpx
    from kalshi_edge.clients import OddsApiClient
    caplog.set_level(logging.INFO)
    create_app(engine=Engine(Settings(), None, DemoKalshi(DemoWorld()), Store(":memory:"), [SPORTS["nfl"]]), run_loop=False)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))) as h:
            await OddsApiClient("TOPSECRET", h).fetch("americanfootball_nfl")
    asyncio.new_event_loop().run_until_complete(go())
    assert "TOPSECRET" not in caplog.text
