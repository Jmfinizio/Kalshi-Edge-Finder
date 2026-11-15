"""FastAPI app: JSON API + static single-page UI. Run with:  python run.py"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .clients import KALSHI_BASE, KalshiClient, OddsApiClient
from .core import Settings
from .demo import DemoKalshi, DemoOdds, DemoWorld
from .service import Engine
from .sports import SPORTS, Sport, parse_kinds, parse_sports
from .store import Store

STATIC = Path(__file__).resolve().parent.parent / "static"


ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Optional[str] = None) -> Optional[Path]:
    """Load KEY=VALUE lines from .env into os.environ. Returns the file used (or None).

    Looks in the current folder first, then next to run.py, so it works wherever you start it from. Tolerates a UTF-8 BOM,
    `export KEY=...`, quotes, and trailing `# comments`. A variable that is set but EMPTY in your shell does not block the file."""
    candidates = [Path(path)] if path else [Path.cwd() / ".env", ROOT / ".env"]
    for p in candidates:
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if v and v[0] in "\"'":
                end = v.find(v[0], 1)
                v = v[1:end] if end > 0 else v[1:]
            else:
                v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
            if k and not os.environ.get(k):
                os.environ[k] = v
        return p
    return None


def demo_reason(key: str, force_demo: bool, env_file: Optional[Path]) -> str:
    if force_demo:
        return "DEMO is set to 1/true/yes (in your .env or your terminal: run `unset DEMO` and remove it from .env)"
    where = f"read {env_file}" if env_file else f"no .env file found (looked in {Path.cwd()} and {ROOT})"
    return f"ODDS_API_KEY is empty or missing: {where}"


class TradeIn(BaseModel):
    ticker: str
    side: str
    qty: int = Field(ge=1, le=100000)
    note: str = ""


class LiveIn(BaseModel):
    enabled: bool
    budget: Optional[int] = Field(default=None, ge=0, le=100000)


class RefreshIn(BaseModel):
    source: str = "both"          # kalshi | book | both
    sport: Optional[str] = None   # omit = every enabled sport


def create_app(engine: Optional[Engine] = None, run_loop: bool = True) -> FastAPI:
    env_file = load_dotenv()
    # httpx logs every request URL at INFO, and The Odds API takes the key in the URL: never let that reach a log.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    own_http: Optional[httpx.AsyncClient] = None
    if engine is None:
        key = os.getenv("ODDS_API_KEY", "").strip()
        force_demo = os.getenv("DEMO", "").lower() in ("1", "true", "yes")
        sports = parse_sports(os.getenv("SPORTS", "nfl"))
        kinds = parse_kinds(os.getenv("COMPARE", "ML"))
        store = Store(os.getenv("DATABASE", "kalshi_edge.db"))
        st = Settings()
        st.update(store.kv_get("settings_patch", {}))                       # settings you saved in the UI survive restarts
        if force_demo or not key:
            world = DemoWorld(sports)
            engine = Engine(st, DemoOdds(world), DemoKalshi(world), store, sports, kinds, demo=True, demo_world=world)
            engine.demo_reason = demo_reason(key, force_demo, env_file)
            logging.getLogger("kalshi_edge").warning("DEMO MODE (fake data). Why: %s", engine.demo_reason)
        else:
            logging.getLogger("kalshi_edge").info("LIVE MODE: ODDS_API_KEY loaded (%d characters) from %s", len(key), env_file or "the environment")
            own_http = httpx.AsyncClient(headers={"User-Agent": "kalshi-edge/1.1"})
            odds = OddsApiClient(key, own_http, regions=os.getenv("ODDS_REGIONS", "us"), bookmakers=os.getenv("ODDS_BOOKMAKERS") or None)
            engine = Engine(st, odds, KalshiClient(own_http, os.getenv("KALSHI_BASE", KALSHI_BASE)), store, sports, kinds)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(engine.run_forever()) if run_loop else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if own_http:
            await own_http.aclose()

    def check_sport(sport: Optional[str]) -> Optional[str]:
        if sport and sport not in engine.states:
            raise HTTPException(404, f"sport '{sport}' is not enabled (enabled: {', '.join(engine.states)})")
        return sport

    app = FastAPI(title="Kalshi vs Sportsbook Edge Finder", lifespan=lifespan)
    app.state.engine = engine

    @app.get("/api/status")
    def status():
        return engine.status()

    @app.get("/api/board")
    def board(sport: Optional[str] = None):
        """Every matched Kalshi contract with its sportsbook fair price (sorted by biggest raw gap)."""
        return {"rows": engine.sport_rows(check_sport(sport)), "scan_ts": engine.now()}

    @app.get("/api/opportunities")
    def opportunities(min_edge: Optional[float] = None, sport: Optional[str] = None):
        return {"rows": engine.opportunities(min_edge, check_sport(sport)), "scan_ts": engine.now()}

    @app.get("/api/diagnostics")
    def diagnostics():
        return {"unmatched_kalshi": engine.unmatched[:300], "unmatched_books": engine.unmatched_games, "errors": list(engine.errors)}

    @app.get("/api/history/{ticker}")
    def history(ticker: str):
        return {"rows": engine.store.history(ticker)}

    @app.post("/api/refresh")
    async def refresh(body: RefreshIn):
        sport = check_sport(body.sport)
        targets = [sport] if sport else list(engine.states)
        if body.source in ("book", "both"):
            if engine.odds is None:
                raise HTTPException(400, "no ODDS_API_KEY configured")
            for k in targets:
                await engine.refresh_book(k, force=True)
        if body.source in ("kalshi", "both"):
            await engine.refresh_kalshi(sport)
        return engine.status()

    @app.get("/api/live")
    def live_status():
        return engine.live_status()

    @app.post("/api/live")
    def live_toggle(body: LiveIn):
        if body.enabled and engine.odds is None:
            raise HTTPException(400, "Live mode needs an ODDS_API_KEY (sportsbook odds are what Kalshi is compared against)")
        engine.set_live(body.enabled, body.budget)
        engine.scan()
        return engine.live_status()

    @app.get("/api/live/board")
    def live_board(sport: Optional[str] = None):
        """Games in progress: Kalshi vs FRESH in-game sportsbook odds. Empty unless Live mode is on."""
        return {"rows": engine.live_rows(check_sport(sport)), "status": engine.live_status(), "now": engine.now()}

    @app.get("/api/settings")
    def get_settings():
        return asdict(engine.st)

    @app.post("/api/settings")
    def set_settings(patch: dict):
        engine.st.update(patch)
        saved = engine.store.kv_get("settings_patch", {})
        saved.update({k: v for k, v in patch.items() if k in asdict(engine.st)})
        engine.store.kv_set("settings_patch", saved)
        engine.scan()
        return asdict(engine.st)

    @app.get("/api/paper-trades")
    def trades(sport: Optional[str] = None, live: Optional[bool] = None):
        return {"rows": engine.store.trades(sport=check_sport(sport), live=live)}

    @app.post("/api/paper-trades")
    def add_trade(t: TradeIn):
        try:
            return engine.paper_trade(t.ticker, t.side, t.qty, t.note)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/paper-trades/{tid}")
    def del_trade(tid: int):
        engine.store.delete_trade(tid)
        return {"ok": True}

    @app.get("/api/quote")
    async def quote(ticker: str, side: str = "YES", qty: int = 100):
        """Live all-in cost of an order: walks Kalshi's real order book (one extra public request, no credits)."""
        try:
            return await engine.order_quote(ticker, side, qty)
        except KeyError:
            raise HTTPException(404, "that market is not in the current scan")
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(502, f"could not read Kalshi's order book: {type(e).__name__}")

    @app.post("/api/settle")
    async def settle():
        return {"settled": await engine.settle()}

    @app.get("/api/performance")
    def performance(sport: Optional[str] = None, live: Optional[bool] = None):
        return engine.performance(check_sport(sport), live)

    @app.post("/api/demo/finish-games")
    async def demo_finish():
        if not engine.demo or engine.demo_world is None:
            raise HTTPException(404, "demo mode only")
        engine.demo_world.finish_all()
        return {"settled": await engine.settle()}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
