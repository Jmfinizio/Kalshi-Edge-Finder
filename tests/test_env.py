"""The .env loader and the 'why am I in demo mode?' explanation."""
import os

import pytest
from fastapi.testclient import TestClient

from kalshi_edge import server
from kalshi_edge.server import create_app, load_dotenv


@pytest.fixture()
def clean(monkeypatch, tmp_path):
    for k in ("ODDS_API_KEY", "DEMO", "SPORTS", "COMPARE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DATABASE", str(tmp_path / "t.db"))
    monkeypatch.setattr(server, "ROOT", tmp_path / "nowhere")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write(p, text, **kw):
    (p / ".env").write_bytes(text.encode(**kw)) if kw else (p / ".env").write_text(text)


def test_plain_key_goes_live(clean):
    write(clean, "ODDS_API_KEY=abc123\nSPORTS=nfl,nhl\n")
    with TestClient(create_app(run_loop=False)) as c:
        s = c.get("/api/status").json()
    assert s["mode"] == "live" and [x["key"] for x in s["sports"]] == ["nfl", "nhl"] and s["demo_reason"] == ""


@pytest.mark.parametrize("text", [
    'ODDS_API_KEY = "abc123"\n', "export ODDS_API_KEY=abc123\n", "ODDS_API_KEY=abc123   # my key\n",
    "ODDS_API_KEY='abc123'\r\nSPORTS=nfl\r\n", "\n# comment\n\nODDS_API_KEY=abc123\n"])
def test_common_formats_all_work(clean, text):
    write(clean, text)
    load_dotenv()
    assert os.environ["ODDS_API_KEY"] == "abc123"


def test_utf8_bom_from_a_text_editor_does_not_hide_the_key(clean):
    write(clean, "\ufeffODDS_API_KEY=abc123\n", encoding="utf-8")          # Notepad-style BOM glued to the first key name
    load_dotenv()
    assert os.environ["ODDS_API_KEY"] == "abc123"


def test_empty_variable_in_shell_does_not_block_the_file(clean, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "")                                  # e.g. an old `export ODDS_API_KEY=`
    write(clean, "ODDS_API_KEY=abc123\n")
    load_dotenv()
    assert os.environ["ODDS_API_KEY"] == "abc123"


def test_real_shell_variable_still_wins_over_file(clean, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "from_shell")
    write(clean, "ODDS_API_KEY=from_file\n")
    load_dotenv()
    assert os.environ["ODDS_API_KEY"] == "from_shell"


def test_finds_env_next_to_run_py_when_started_from_elsewhere(clean, monkeypatch, tmp_path):
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / ".env").write_text("ODDS_API_KEY=abc123\n")
    monkeypatch.setattr(server, "ROOT", proj)
    elsewhere = tmp_path / "elsewhere"; elsewhere.mkdir(); monkeypatch.chdir(elsewhere)
    assert load_dotenv() == proj / ".env" and os.environ["ODDS_API_KEY"] == "abc123"


@pytest.mark.parametrize("text,needle", [
    ("", "no .env file found"),                                              # no file at all
    ("SPORTS=nfl\n", "ODDS_API_KEY is empty or missing: read"),             # file found, key line absent
    ("ODDS_API_KEY=\n", "ODDS_API_KEY is empty or missing: read"),          # key line present but blank
    ("# ODDS_API_KEY=abc123\n", "ODDS_API_KEY is empty or missing: read"),  # key line commented out
    ("ODDS_API_KEY=abc123\nDEMO=1\n", "DEMO is set")])                       # forced demo
def test_demo_mode_always_says_why(clean, text, needle):
    if text:
        write(clean, text)
    with TestClient(create_app(run_loop=False)) as c:
        s = c.get("/api/status").json()
    assert s["mode"] == "demo" and needle in s["demo_reason"], s["demo_reason"]


def test_demo_zero_does_not_force_demo(clean):
    write(clean, "ODDS_API_KEY=abc123\nDEMO=0\n")
    with TestClient(create_app(run_loop=False)) as c:
        assert c.get("/api/status").json()["mode"] == "live"
