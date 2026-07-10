"""TENNIS-PROVIDER-001 tests: provider adapter scaffold (default-off).

Fixture→scoreboard adaptation from fake API-Tennis responses, Kalshi code
derivation, tour filtering, no-key → no request (honest provider_gap),
provider selection wiring, end-to-end compatibility with the existing
tennis-live-source matching, secret hygiene, no forbidden vocabulary. No real
network request is ever made.
"""

import asyncio
from pathlib import Path

import pytest

from app.config import Settings
from app.services.tennis_providers import (
    API_TENNIS_SOURCE,
    ApiTennisFetcher,
    adapt_fixtures_to_scoreboard,
    kalshi_code,
)
from app.services.tennis_research import _find_event, get_tennis_fetcher, parse_tennis_ticker

REPO = Path(__file__).resolve().parents[1]


def fixture(first="Hugo Casanova", second="Thiago Ambrosio",
            status="Set 2", event_type="Challenger Men Singles"):
    return {
        "event_key": 111,
        "event_first_player": first,
        "event_second_player": second,
        "event_status": status,
        "event_type_type": event_type,
        "tournament_name": "Some Challenger",
    }


class TestKalshiCode:
    def test_last_name_prefix(self):
        assert kalshi_code("Jannik Sinner") == "SIN"
        assert kalshi_code("Carlos Alcaraz Garfia") == "GAR"   # last token, honestly
        assert kalshi_code("Casanova") == "CAS"

    def test_missing_name(self):
        assert kalshi_code(None) == ""
        assert kalshi_code("  ") == ""


class TestAdaptation:
    def test_fixture_maps_to_scoreboard_event(self):
        board = adapt_fixtures_to_scoreboard({"result": [fixture()]}, tour="atp")
        assert len(board["events"]) == 1
        e = board["events"][0]
        abbrs = [c["athlete"]["abbreviation"]
                 for c in e["competitions"][0]["competitors"]]
        assert abbrs == ["CAS", "AMB"]
        assert e["status"]["type"]["state"] == "in"           # "Set 2" = live
        assert e["provider_meta"]["source"] == API_TENNIS_SOURCE

    def test_state_mapping(self):
        for status, state in (
            ("Finished", "post"), ("Retired", "post"),
            ("Set 1", "in"), ("Live", "in"),
            ("", "unknown"), ("Not Started", "pre"),
        ):
            board = adapt_fixtures_to_scoreboard(
                {"result": [fixture(status=status)]}, tour="atp"
            )
            assert board["events"][0]["status"]["type"]["state"] == state, status

    def test_missing_player_skipped_never_padded(self):
        board = adapt_fixtures_to_scoreboard(
            {"result": [fixture(second=None)]}, tour="atp"
        )
        assert board["events"] == []

    def test_tour_filter(self):
        men = fixture(event_type="Challenger Men Singles")
        women = fixture(first="Iga Swiatek", second="Coco Gauff",
                        event_type="Wta Singles")
        board_atp = adapt_fixtures_to_scoreboard({"result": [men, women]}, "atp")
        board_wta = adapt_fixtures_to_scoreboard({"result": [men, women]}, "wta")
        assert len(board_atp["events"]) == 1
        assert len(board_wta["events"]) == 1
        assert board_wta["events"][0]["competitions"][0]["competitors"][0][
            "athlete"]["displayName"] == "Iga Swiatek"

    def test_none_payload(self):
        assert adapt_fixtures_to_scoreboard(None, "atp") is None

    def test_adapted_event_matches_existing_finder(self):
        # the whole point of the shape: TENNIS-001's _find_event matches it
        board = adapt_fixtures_to_scoreboard({"result": [fixture()]}, "atp")
        ctx = parse_tennis_ticker("KXATPCHALLENGERMATCH-26JUL10CASAMB-CAS")
        assert ctx is not None
        assert _find_event(board, ctx) is not None

    def test_four_letter_codes_do_not_match_v1_limitation(self):
        board = adapt_fixtures_to_scoreboard({"result": [fixture()]}, "atp")
        ctx = parse_tennis_ticker("KXATPCHALLENGERMATCH-26JUL10CASAAMBR-CASA")
        assert ctx is not None
        assert _find_event(board, ctx) is None   # documented v1 limitation


class TestNoKeyNoRequest:
    def test_fetch_without_key_returns_none_without_network(self):
        fetcher = ApiTennisFetcher(api_key="")
        assert fetcher.has_key is False
        # _get guards before any httpx client is created
        result = asyncio.run(fetcher.fetch_scoreboard("atp", "2026-07-10"))
        assert result is None

    def test_key_never_in_display_url(self):
        fetcher = ApiTennisFetcher(api_key="SECRET123")
        assert "SECRET123" not in fetcher.scoreboard_url("atp", "2026-07-10")
        assert "SECRET123" not in fetcher.match_details_url("atp", "111")


class TestProviderSelection:
    def test_api_tennis_selected(self):
        s = Settings(_env_file=None, tennis_research_provider="api_tennis")
        f = get_tennis_fetcher(s)
        assert isinstance(f, ApiTennisFetcher)
        assert f.has_key is False               # default: no key, no request

    def test_default_remains_template(self):
        s = Settings(_env_file=None)
        assert s.tennis_research_provider == "template"
        assert s.tennis_provider_api_key == ""  # no secret committed
        assert get_tennis_fetcher(s) is None

    def test_key_flows_from_settings(self):
        s = Settings(
            _env_file=None, tennis_research_provider="api_tennis",
            tennis_provider_api_key="k",
        )
        assert get_tennis_fetcher(s).has_key is True


class TestSafety:
    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "tennis_providers.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "jupiter", "recommend",
                    "execute_trade", "execution", "buy", "sell", "odds"):
            assert bad not in code, bad

    def test_research_doc_exists_with_required_sections(self):
        doc = (REPO / "docs" / "TENNIS_PROVIDER_RESEARCH_2026_07_10.md").read_text()
        for required in ("Provider comparison", "Recommendation",
                         "Bounded validation plan", "TENNIS-TAPE-001",
                         "authorizes nothing"):
            assert required in doc, required
