"""POLY-COVERAGE-001 tests: read-only Polymarket coverage expansion.

Covers bounded catalog pagination (offset walk, budget/page ceilings, short-page
stop), category + resolution-window filters, public-search flattening (including
the parent-event endDate/category inheritance without which every search-sourced
market would be structurally unmatchable), de-duplication across pages and across
search+catalog, the bounded order-book budget, provider-error accounting (a
provider problem is an error; an exhausted catalog is not), deterministic
Kalshi-derived targeted queries, the coverage report, the CLI options, migration
0022 up/down, and — critically — that NO arbitrage/EV/trade/side/size/wallet
surface exists. No live network anywhere; in-memory SQLite.
"""

import asyncio
import inspect as pyinspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

import app.config as config_module
from app import cli
from app.adapters import polymarket as poly_adapter
from app.adapters.polymarket import (
    MAX_CATALOG_PAGES,
    MAX_PAGE_SIZE,
    MAX_SEARCH_PAGES,
    MAX_TOTAL_MARKETS,
    MarketFetchResult,
    PolymarketAdapter,
    PolymarketMarketData,
    PolymarketOrderbook,
    _markets_from_search,
    _search_market_payload,
)
from app.config import Settings
from app.db import Base
from app.models import Market, MarketSnapshot, PolymarketMarket, PolymarketScoutRun
from app.services import polymarket as poly_service
from app.services.polymarket import (
    SCAN_MODE_BOTH,
    SCAN_MODE_CATALOG,
    TARGETED_TOPICS,
    PolymarketConfig,
    PolymarketScoutService,
    derive_targeted_queries,
)
from app.services.polymarket_coverage import (
    REASON_NO_KALSHI,
    REASON_NO_POLY_RESOLUTION,
    REASON_NO_POLYMARKET,
    PolymarketCoverageReportService,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def market(market_id, *, question=None, active=True, closed=False, ob=True,
           end=True, two_sided=True, liq=1000.0, category="World Cup Winner"):
    return PolymarketMarketData(
        market_id=str(market_id),
        condition_id="0x" + str(market_id),
        question=question or f"Will team {market_id} win the 2026 FIFA World Cup?",
        slug=f"s-{market_id}",
        category=category,
        description="d",
        active=active,
        closed=closed,
        archived=False,
        restricted=False,
        enable_order_book=ob,
        accepting_orders=True,
        outcomes=["Yes", "No"],
        outcome_prices=[0.4, 0.6],
        clob_token_ids=[f"t{market_id}a", f"t{market_id}b"],
        best_bid=0.39 if two_sided else None,
        best_ask=0.41 if two_sided else None,
        last_trade_price=0.40,
        spread=0.02,
        liquidity_usd=liq,
        volume_24h_usd=liq,
        volume_total_usd=liq * 10,
        start_date=NOW,
        end_date=(NOW + timedelta(days=5)) if end else None,
    )


def orderbook(token_id):
    return PolymarketOrderbook(
        token_id=token_id, market="m", best_bid=0.39, best_ask=0.41, mid=0.40,
        spread=0.02, bid_depth=300.0, ask_depth=200.0, total_depth=500.0,
        num_bids=2, num_asks=2, liquidity_proxy=200.0, tick_size=0.01,
    )


class PagedAdapter(PolymarketAdapter):
    """Real adapter with a fake page source. `_get` raises, so no test can
    reach the network."""

    def __init__(self, pages=None, search=None, books=None, fail_page=None):
        super().__init__(settings=Settings(_env_file=None))
        self._pages = pages if pages is not None else []
        self._search = search or {}
        self._books = books or {}
        self._fail_page = fail_page  # page index whose fetch simulates a provider problem
        self.page_calls = []
        self.search_calls = []
        self.orderbook_calls = []

    async def _get(self, base, path, params=None):  # pragma: no cover - network guard
        raise AssertionError(f"tests must not perform live calls (attempted {base}{path})")

    async def fetch_markets_page(self, limit=50, active=True, closed=False, offset=0,
                                 tag_id=None, end_date_min=None, end_date_max=None):
        self.page_calls.append({"limit": limit, "offset": offset, "tag_id": tag_id,
                                "active": active, "closed": closed,
                                "end_date_min": end_date_min, "end_date_max": end_date_max})
        index = offset // max(1, limit)
        if self._fail_page is not None and index == self._fail_page:
            return None
        return self._pages[index] if index < len(self._pages) else []

    async def search_markets(self, query, limit_per_type=20, max_pages=MAX_SEARCH_PAGES,
                             active_only=True, include_closed=False,
                             end_date_min=None, end_date_max=None):
        self.search_calls.append(query)
        return MarketFetchResult(markets=list(self._search.get(query, [])), pages_fetched=1)

    async def fetch_orderbook(self, token_id):
        self.orderbook_calls.append(token_id)
        return self._books.get(token_id)


def service_with(adapter, **cfg):
    base = dict(market_limit=50, orderbook_limit=20, provider_version="v1",
                page_size=10, max_pages=5, search_limit_per_type=20,
                search_max_pages=3, max_targeted_queries=6)
    base.update(cfg)
    return PolymarketScoutService(adapter=adapter, config=PolymarketConfig(**base))


# --- bounded pagination ------------------------------------------------------


class TestPagination:
    def test_walks_pages_with_offset_until_budget(self):
        pages = [[market(f"p{p}{i}") for i in range(10)] for p in range(4)]
        a = PagedAdapter(pages=pages)
        r = asyncio.run(a.fetch_market_catalog(total_limit=25, page_size=10, max_pages=5))

        assert len(r.markets) == 25
        assert r.truncated is True
        assert [c["offset"] for c in a.page_calls] == [0, 10, 20]
        assert r.provider_errors == 0

    def test_short_page_stops_the_walk_without_error(self):
        a = PagedAdapter(pages=[[market(f"a{i}") for i in range(10)], [market("b0"), market("b1")]])
        r = asyncio.run(a.fetch_market_catalog(total_limit=100, page_size=10, max_pages=5))

        assert len(r.markets) == 12
        assert r.pages_fetched == 2
        assert r.provider_errors == 0  # exhausted catalog is NOT a provider error

    def test_exhausted_catalog_is_not_a_provider_error(self):
        a = PagedAdapter(pages=[[market("only")]])
        r = asyncio.run(a.fetch_market_catalog(total_limit=100, page_size=1, max_pages=5))
        # page 0 -> 1 row (full page), page 1 -> [] (exhausted)
        assert len(r.markets) == 1
        assert r.provider_errors == 0

    def test_max_pages_is_respected(self):
        pages = [[market(f"p{p}{i}") for i in range(10)] for p in range(10)]
        a = PagedAdapter(pages=pages)
        r = asyncio.run(a.fetch_market_catalog(total_limit=1000, page_size=10, max_pages=2))

        assert r.pages_fetched == 2
        assert len(r.markets) == 20

    def test_hard_ceilings_clamp_caller_values(self):
        pages = [[market(f"p{p}{i}") for i in range(3)] for p in range(3)]
        a = PagedAdapter(pages=pages)
        asyncio.run(a.fetch_market_catalog(total_limit=10**9, page_size=10**9, max_pages=10**9))

        assert a.page_calls[0]["limit"] == MAX_PAGE_SIZE
        assert len(a.page_calls) <= MAX_CATALOG_PAGES
        assert MAX_TOTAL_MARKETS == 1000

    def test_deduplicates_repeated_market_ids_across_pages(self):
        dup = market("same")
        a = PagedAdapter(pages=[[dup, market("x")], [dup, market("y")]])
        r = asyncio.run(a.fetch_market_catalog(total_limit=100, page_size=2, max_pages=5))

        assert [m.market_id for m in r.markets] == ["same", "x", "y"]
        assert r.duplicates_dropped == 1

    def test_provider_problem_counts_as_error_and_stops(self):
        pages = [[market(f"a{i}") for i in range(5)], [market("never")]]
        a = PagedAdapter(pages=pages, fail_page=1)
        r = asyncio.run(a.fetch_market_catalog(total_limit=100, page_size=5, max_pages=5))

        assert r.provider_errors == 1
        assert len(r.markets) == 5  # stopped, did not hammer the endpoint
        assert len(a.page_calls) == 2

    def test_category_and_resolution_window_forwarded(self):
        a = PagedAdapter(pages=[[market("x")]])
        asyncio.run(a.fetch_market_catalog(
            total_limit=1, page_size=1, tag_id=42,
            end_date_min="2026-07-08T00:00:00Z", end_date_max="2026-07-20T00:00:00Z",
        ))
        call = a.page_calls[0]
        assert call["tag_id"] == 42
        assert call["end_date_min"] == "2026-07-08T00:00:00Z"
        assert call["end_date_max"] == "2026-07-20T00:00:00Z"


# --- public search -----------------------------------------------------------

SEARCH_EVENT = {
    "id": "556270",
    "title": "World Cup: Golden Ball Winner",
    "ticker": "wc-golden-ball",
    "slug": "wc-golden-ball",
    "endDate": "2026-07-20T03:59:00Z",
    "markets": [
        {
            "id": "2431009",
            "conditionId": "0xabc",
            "question": "Will Lionel Messi win the Golden Ball?",
            "slug": "messi-golden-ball",
            "active": True,
            "closed": False,
            "enableOrderBook": True,
            "acceptingOrders": True,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.2", "0.8"]',
            "clobTokenIds": '["tok1", "tok2"]',
            "bestBid": "0.19",
            "bestAsk": "0.21",
            "spread": "0.02",
            "liquidityNum": 5000.0,
            "volume24hr": 1000.0,
            # NOTE: no "endDate" and no "events" — exactly what the live
            # /public-search endpoint returns for a nested market.
        }
    ],
}


class TestSearch:
    def test_nested_market_inherits_event_resolution_time(self):
        """Without this, a search-sourced market has no resolution time, and the
        POLY-002 matcher can NEVER label it comparable — it would silently fall
        to unresolved_semantic_match forever."""
        payload = _search_market_payload(SEARCH_EVENT, SEARCH_EVENT["markets"][0])
        assert payload["endDate"] == "2026-07-20T03:59:00Z"

        parsed = _markets_from_search({"events": [SEARCH_EVENT]})
        assert len(parsed) == 1
        assert parsed[0].end_date is not None

    def test_nested_market_inherits_event_category(self):
        parsed = _markets_from_search({"events": [SEARCH_EVENT]})
        assert parsed[0].category == "World Cup: Golden Ball Winner"

    def test_market_own_end_date_is_not_overwritten(self):
        own = dict(SEARCH_EVENT["markets"][0], endDate="2026-01-01T00:00:00Z")
        payload = _search_market_payload(SEARCH_EVENT, own)
        assert payload["endDate"] == "2026-01-01T00:00:00Z"

    def test_missing_on_both_stays_missing(self):
        event = dict(SEARCH_EVENT, endDate=None)
        payload = _search_market_payload(event, SEARCH_EVENT["markets"][0])
        assert not payload.get("endDate")

    def test_search_tolerates_schema_drift(self):
        assert _markets_from_search({}) == []
        assert _markets_from_search({"events": "nope"}) == []
        assert _markets_from_search({"events": [{"markets": [{"no_id": 1}]}]}) == []

    def test_search_paginates_with_page_not_offset(self):
        """/public-search IGNORES `offset`; paginating with it silently re-fetches
        page 1 forever. Assert we send `page`."""
        calls = []

        class SearchAdapter(PolymarketAdapter):
            async def _get(self, base, path, params=None):
                calls.append(params)
                if params["page"] == 1:
                    return {"events": [SEARCH_EVENT], "pagination": {"hasMore": True}}
                return {"events": [], "pagination": {"hasMore": False}}

        a = SearchAdapter(settings=Settings(_env_file=None))
        r = asyncio.run(a.search_markets("world cup", max_pages=3))

        assert [c["page"] for c in calls] == [1, 2]
        assert all("offset" not in c for c in calls)
        assert len(r.markets) == 1

    def test_search_stops_when_has_more_false(self):
        class SearchAdapter(PolymarketAdapter):
            async def _get(self, base, path, params=None):
                return {"events": [SEARCH_EVENT], "pagination": {"hasMore": False}}

        a = SearchAdapter(settings=Settings(_env_file=None))
        r = asyncio.run(a.search_markets("world cup", max_pages=5))
        assert r.pages_fetched == 1

    def test_search_provider_problem_counted(self):
        class SearchAdapter(PolymarketAdapter):
            async def _get(self, base, path, params=None):
                return None

        a = SearchAdapter(settings=Settings(_env_file=None))
        r = asyncio.run(a.search_markets("world cup"))
        assert r.provider_errors == 1
        assert r.markets == []

    def test_search_filters_closed_and_inactive_client_side(self):
        inactive = dict(SEARCH_EVENT["markets"][0], id="inact", active=False)
        closed = dict(SEARCH_EVENT["markets"][0], id="clsd", closed=True)
        event = dict(SEARCH_EVENT, markets=[SEARCH_EVENT["markets"][0], inactive, closed])

        class SearchAdapter(PolymarketAdapter):
            async def _get(self, base, path, params=None):
                return {"events": [event], "pagination": {"hasMore": False}}

        a = SearchAdapter(settings=Settings(_env_file=None))
        r = asyncio.run(a.search_markets("q", active_only=True, include_closed=False))
        assert [m.market_id for m in r.markets] == ["2431009"]

        r2 = asyncio.run(a.search_markets("q", active_only=False, include_closed=True))
        assert {m.market_id for m in r2.markets} == {"2431009", "inact", "clsd"}

    def test_blank_query_makes_no_request(self):
        a = PagedAdapter()
        r = asyncio.run(PolymarketAdapter.search_markets(a, "   "))
        assert r == MarketFetchResult()

    def test_resolution_window_filters_search_client_side(self):
        """/public-search exposes no date parameter, so the window MUST be applied
        client-side — otherwise --end-date-min/max would be silently ignored for
        targeted queries."""
        near = dict(SEARCH_EVENT["markets"][0], id="near", endDate="2026-07-10T00:00:00Z")
        far = dict(SEARCH_EVENT["markets"][0], id="far", endDate="2026-12-01T00:00:00Z")
        event = dict(SEARCH_EVENT, endDate=None, markets=[near, far])

        class SearchAdapter(PolymarketAdapter):
            async def _get(self, base, path, params=None):
                return {"events": [event], "pagination": {"hasMore": False}}

        a = SearchAdapter(settings=Settings(_env_file=None))
        r = asyncio.run(a.search_markets(
            "q", end_date_min="2026-07-08T00:00:00Z", end_date_max="2026-07-20T00:00:00Z"))
        assert [m.market_id for m in r.markets] == ["near"]

    def test_market_without_resolution_time_excluded_from_window(self):
        """A market with no end date cannot be SHOWN to resolve in-window, so it
        is excluded rather than optimistically admitted."""
        undated = dict(SEARCH_EVENT["markets"][0], id="undated")
        event = dict(SEARCH_EVENT, endDate=None, markets=[undated])

        class SearchAdapter(PolymarketAdapter):
            async def _get(self, base, path, params=None):
                return {"events": [event], "pagination": {"hasMore": False}}

        a = SearchAdapter(settings=Settings(_env_file=None))
        assert asyncio.run(a.search_markets("q", end_date_min="2026-07-08T00:00:00Z")).markets == []
        assert len(asyncio.run(a.search_markets("q")).markets) == 1  # no window -> kept

    def test_window_handles_naive_and_aware_timestamps(self):
        """Gamma mixes '...Z' and naive timestamps; comparing them must not raise."""
        naive = dict(SEARCH_EVENT["markets"][0], id="naive", endDate="2026-07-10T00:00:00")
        event = dict(SEARCH_EVENT, endDate=None, markets=[naive])

        class SearchAdapter(PolymarketAdapter):
            async def _get(self, base, path, params=None):
                return {"events": [event], "pagination": {"hasMore": False}}

        a = SearchAdapter(settings=Settings(_env_file=None))
        r = asyncio.run(a.search_markets("q", end_date_min="2026-07-08T00:00:00Z"))
        assert [m.market_id for m in r.markets] == ["naive"]


# --- merge / dedupe ----------------------------------------------------------


class TestMerge:
    def test_merge_dedupes_first_wins(self):
        a = MarketFetchResult(markets=[market("x"), market("y")])
        b = MarketFetchResult(markets=[market("y"), market("z")])
        m = a.merge(b)
        assert [x.market_id for x in m.markets] == ["x", "y", "z"]
        assert m.duplicates_dropped == 1

    def test_merge_sums_counters(self):
        a = MarketFetchResult(pages_fetched=2, provider_errors=1)
        b = MarketFetchResult(pages_fetched=3, provider_errors=1, truncated=True)
        m = a.merge(b)
        assert (m.pages_fetched, m.provider_errors, m.truncated) == (5, 2, True)


# --- targeted discovery ------------------------------------------------------


def kalshi(session, ticker, title, *, category="Sports", days=5, status="active"):
    m = Market(ticker=ticker, event_ticker=ticker.split("-")[0], title=title,
               category=category, status=status, close_time=NOW + timedelta(days=days))
    session.add(m)
    session.flush()
    session.add(MarketSnapshot(market_id=m.id, yes_bid=20, yes_ask=24,
                               liquidity=5000, volume_24h=5000, captured_at=NOW))
    return m


class TestTargetedQueries:
    def test_derives_only_topics_kalshi_evidences(self, session):
        kalshi(session, "KXWC-1", "France to win the 2026 FIFA World Cup")
        kalshi(session, "KXWC-2", "Argentina to win the World Cup")
        kalshi(session, "KXMLB-1", "Will the Yankees win? MLB game")
        session.commit()

        qs = derive_targeted_queries(session)
        assert "world cup" in qs
        assert "mlb" in qs
        assert "wimbledon" not in qs  # no Kalshi evidence -> never invented

    def test_ranked_by_evidence_count_then_name(self, session):
        for i in range(3):
            kalshi(session, f"KXWC-{i}", "World Cup winner")
        kalshi(session, "KXT-1", "Wimbledon final")
        session.commit()

        assert derive_targeted_queries(session)[0] == "world cup"

    def test_deterministic_for_same_db_state(self, session):
        kalshi(session, "KXWC-1", "World Cup")
        kalshi(session, "KXE-1", "Presidential election winner")
        session.commit()
        assert derive_targeted_queries(session) == derive_targeted_queries(session)

    def test_single_word_terms_match_whole_words_only(self, session):
        # "fed" must not be evidenced by "federal"
        kalshi(session, "KXF-1", "Will the federal building open?", category="Other")
        session.commit()
        assert "fed" not in derive_targeted_queries(session)

        kalshi(session, "KXF-2", "Will the Fed cut rates?", category="Economics")
        session.commit()
        assert "fed" in derive_targeted_queries(session)

    def test_ticker_prefix_evidences_topic_when_title_does_not(self, session):
        """Real Kalshi rows carry the series ONLY in the ticker: `category` is
        empty and the title is game-prop text. Title-only evidence would skip
        ~1100 active World Cup and ~1160 tennis markets outright."""
        kalshi(session, "KXWC2HTOTAL-26JUL06PORESP-4", "Over 3.5 2H goals scored?", category=None)
        kalshi(session, "KXITFMATCH-26JUL08-ABC", "Will player A win the match?", category=None)
        session.commit()

        qs = derive_targeted_queries(session)
        assert "world cup" in qs
        assert "tennis" in qs

    def test_ticker_evidence_is_prefix_anchored_not_substring(self, session):
        """A substring test for "FED" matches the MLB ticker of pitcher Erick
        Fedde. Only a prefix-anchored match is safe."""
        kalshi(session, "KXMLBRBI-26JUL08-FEDDE", "Will Fedde record an RBI?", category=None)
        session.commit()

        qs = derive_targeted_queries(session)
        assert "fed" not in qs
        assert "mlb" in qs  # the KXMLB prefix, correctly

    def test_no_kalshi_markets_yields_no_queries(self, session):
        assert derive_targeted_queries(session) == []

    def test_max_queries_bounds_output(self, session):
        kalshi(session, "KXA-1", "World Cup MLB bitcoin ethereum crypto election wimbledon tennis")
        session.commit()
        assert len(derive_targeted_queries(session, max_queries=2)) == 2

    def test_topics_use_no_llm_and_no_external_source(self):
        """Targeting is pure DB + string matching. Strip the docstring first —
        it legitimately says "no LLM" in order to disclaim one."""
        fn = poly_service.derive_targeted_queries
        src = pyinspect.getsource(fn).replace(fn.__doc__ or "", "").lower()
        for bad in ("anthropic", "openai", "llm", "httpx", "requests", "await"):
            assert bad not in src
        assert len(TARGETED_TOPICS) >= 5


# --- scan integration --------------------------------------------------------


class TestScanCoverage:
    def test_targeted_queries_claim_budget_before_catalog(self, session):
        kalshi(session, "KXWC-1", "France to win the 2026 FIFA World Cup")
        session.commit()
        a = PagedAdapter(
            pages=[[market(f"cat{i}") for i in range(10)]],
            search={"world cup": [market("wc1"), market("wc2")]},
        )
        run = asyncio.run(service_with(a).scan_once(session, limit=5, targeted=True))

        ids = [m.market_id for m in session.query(PolymarketMarket).all()]
        assert "wc1" in ids and "wc2" in ids       # targeted supply persisted first
        assert run.markets_seen == 5
        assert run.scan_mode == SCAN_MODE_BOTH
        assert run.queries_used == ["world cup"]

    def test_explicit_queries_are_used(self, session):
        a = PagedAdapter(pages=[[]], search={"tennis": [market("t1")]})
        run = asyncio.run(service_with(a).scan_once(session, limit=10, queries=["tennis"]))
        assert a.search_calls == ["tennis"]
        assert run.queries_used == ["tennis"]

    def test_one_high_yield_query_cannot_starve_the_others(self, session):
        """A single "mlb" search returns hundreds of season/draft futures. Without
        a fair share it consumes the whole scan and no other topic is ever
        fetched — the opposite of targeted discovery."""
        a = PagedAdapter(
            pages=[[]],
            search={
                "mlb": [market(f"mlb{i}") for i in range(100)],
                "world cup": [market(f"wc{i}") for i in range(10)],
            },
        )
        run = asyncio.run(service_with(a).scan_once(
            session, limit=20, queries=["mlb", "world cup"]))

        ids = {m.market_id for m in session.query(PolymarketMarket).all()}
        assert a.search_calls == ["mlb", "world cup"]
        assert any(i.startswith("wc") for i in ids), "world cup was starved"
        assert sum(i.startswith("mlb") for i in ids) == 10  # capped at its fair share
        assert run.markets_seen == 20

    def test_underspending_query_hands_slack_to_the_next(self, session):
        a = PagedAdapter(
            pages=[[]],
            search={"thin": [market("t1")], "fat": [market(f"f{i}") for i in range(50)]},
        )
        asyncio.run(service_with(a).scan_once(session, limit=20, queries=["thin", "fat"]))

        ids = {m.market_id for m in session.query(PolymarketMarket).all()}
        assert "t1" in ids
        assert sum(i.startswith("f") for i in ids) == 19  # took the leftover slack

    def test_window_forwarded_to_search(self, session):
        captured = {}

        class WindowAdapter(PagedAdapter):
            async def search_markets(self, query, limit_per_type=20, max_pages=3,
                                     active_only=True, include_closed=False,
                                     end_date_min=None, end_date_max=None):
                captured["min"], captured["max"] = end_date_min, end_date_max
                return MarketFetchResult(markets=[market("x")], pages_fetched=1)

        a = WindowAdapter(pages=[[]])
        asyncio.run(service_with(a).scan_once(
            session, limit=5, queries=["q"],
            end_date_min="2026-07-08T00:00:00Z", end_date_max="2026-07-20T00:00:00Z"))
        assert captured == {"min": "2026-07-08T00:00:00Z", "max": "2026-07-20T00:00:00Z"}

    def test_budget_starved_query_is_not_reported_as_used(self, session):
        """`queries_used` is an audit field: it must record what was SENT, not
        what was planned. A query skipped for budget was never fetched, so the
        run must not claim coverage of it."""
        a = PagedAdapter(
            pages=[[]],
            search={"first": [market(f"f{i}") for i in range(5)], "second": [market("s1")]},
        )
        # limit=1 -> "first" claims the only slot, "second" is never sent
        run = asyncio.run(service_with(a).scan_once(session, limit=1, queries=["first", "second"]))

        assert a.search_calls == ["first"]          # "second" never sent
        assert run.queries_used == ["first"]
        assert run.markets_seen == 1

    def test_catalog_only_scan_mode(self, session):
        a = PagedAdapter(pages=[[market("c1")]])
        run = asyncio.run(service_with(a).scan_once(session, limit=5))
        assert run.scan_mode == SCAN_MODE_CATALOG
        assert run.queries_used is None

    def test_dedupes_between_search_and_catalog(self, session):
        shared = market("shared")
        a = PagedAdapter(pages=[[shared, market("only_cat")]], search={"q": [shared]})
        run = asyncio.run(service_with(a).scan_once(session, limit=10, queries=["q"]))

        ids = [m.market_id for m in session.query(PolymarketMarket).all()]
        assert sorted(ids) == ["only_cat", "shared"]
        assert run.duplicates_dropped >= 1

    def test_orderbook_budget_bounded_even_with_many_markets(self, session):
        pages = [[market(f"m{i}") for i in range(10)]]
        books = {f"t{f'm{i}'}{s}": orderbook(f"t{f'm{i}'}{s}") for i in range(10) for s in "ab"}
        a = PagedAdapter(pages=pages, books=books)
        run = asyncio.run(service_with(a).scan_once(session, limit=10, orderbook_limit=3))

        assert len(a.orderbook_calls) == 3
        assert run.orderbooks_fetched == 3

    def test_orderbook_limit_zero_fetches_nothing(self, session):
        a = PagedAdapter(pages=[[market("m1")]], books={"tm1a": orderbook("tm1a")})
        run = asyncio.run(service_with(a).scan_once(session, limit=5, orderbook_limit=0))
        assert a.orderbook_calls == []
        assert run.orderbooks_fetched == 0

    def test_provider_error_recorded_and_run_still_ok(self, session):
        a = PagedAdapter(pages=[[market("a")]], fail_page=0)
        run = asyncio.run(service_with(a).scan_once(session, limit=10))

        assert run.status == "ok"           # provider outage != run failure
        assert run.market_fetch_errors == 1
        assert run.markets_seen == 0

    def test_category_and_window_reach_the_adapter(self, session):
        a = PagedAdapter(pages=[[market("x")]])
        asyncio.run(service_with(a).scan_once(
            session, limit=1, tag_id=7,
            end_date_min="2026-07-08T00:00:00Z", end_date_max="2026-07-20T00:00:00Z",
        ))
        assert a.page_calls[0]["tag_id"] == 7
        assert a.page_calls[0]["end_date_min"] == "2026-07-08T00:00:00Z"

    def test_include_closed_forwarded(self, session):
        a = PagedAdapter(pages=[[market("x")]])
        asyncio.run(service_with(a).scan_once(session, limit=1, include_closed=True, active_only=False))
        assert a.page_calls[0]["closed"] is True
        assert a.page_calls[0]["active"] is False

    def test_pages_fetched_recorded(self, session):
        a = PagedAdapter(pages=[[market(f"p{i}") for i in range(3)] for _ in range(2)])
        run = asyncio.run(service_with(a, page_size=3).scan_once(session, limit=100))
        assert run.pages_fetched >= 1


# --- coverage report ---------------------------------------------------------


def poly_row(session, mid, question, *, category="World Cup Winner", end=True,
             ob=True, two_sided=True, active=True, liq=1000.0):
    session.add(PolymarketMarket(
        market_id=mid, condition_id="0x" + mid, question=question, category=category,
        active=active, closed=False, archived=False, restricted=False,
        enable_order_book=ob, accepting_orders=True,
        outcomes=["Yes", "No"], outcome_prices=[0.4, 0.6], clob_token_ids=[f"t{mid}"],
        num_outcomes=2, best_bid=0.39 if two_sided else None,
        best_ask=0.41 if two_sided else None, spread=0.02, two_sided=two_sided,
        liquidity_usd=liq, volume_24h_usd=liq,
        end_date=(NOW + timedelta(days=5)) if end else None,
        observed_at=NOW, created_at=NOW,
    ))


class TestCoverageReport:
    def test_reports_supply_per_domain(self, session):
        kalshi(session, "KXWC-1", "France to win the 2026 FIFA World Cup")
        poly_row(session, "p1", "Will France win the 2026 FIFA World Cup?")
        session.commit()

        r = PolymarketCoverageReportService().build(session)
        sports = next(d for d in r.domains if d["domain"] == "sports")
        assert sports["polymarket_markets"] == 1
        assert sports["kalshi_markets"] == 1
        assert sports["comparable_supply"] is True
        assert "sports" in r.overlap_domains
        assert "sports" in r.comparable_supply_domains

    def test_domain_without_polymarket_supply_is_diagnosed(self, session):
        kalshi(session, "KXE-1", "Who will win the presidential election?", category="Politics")
        session.commit()

        r = PolymarketCoverageReportService().build(session)
        politics = next(d for d in r.no_comparable_supply_domains if d["domain"] == "politics")
        assert REASON_NO_POLYMARKET in politics["reasons"]

    def test_domain_without_kalshi_supply_is_diagnosed(self, session):
        poly_row(session, "p1", "Will Bitcoin hit 200k?", category="Crypto")
        session.commit()

        r = PolymarketCoverageReportService().build(session)
        crypto = next(d for d in r.no_comparable_supply_domains if d["domain"] == "crypto")
        assert REASON_NO_KALSHI in crypto["reasons"]

    def test_missing_resolution_time_is_diagnosed(self, session):
        kalshi(session, "KXWC-1", "France to win the 2026 FIFA World Cup")
        poly_row(session, "p1", "Will France win the 2026 FIFA World Cup?", end=False)
        session.commit()

        r = PolymarketCoverageReportService().build(session)
        sports = next(d for d in r.no_comparable_supply_domains if d["domain"] == "sports")
        assert REASON_NO_POLY_RESOLUTION in sports["reasons"]

    def test_market_types_reported_for_both_venues(self, session):
        kalshi(session, "KXWC-1", "France to win the 2026 FIFA World Cup")
        poly_row(session, "p1", "Switzerland vs. Colombia: Over 2.5 Goals")
        session.commit()

        r = PolymarketCoverageReportService().build(session)
        assert r.polymarket_market_types.get("over_under") == 1
        assert r.kalshi_market_types.get("winner") == 1

    def test_market_types_reuse_the_matcher_vocabulary(self, session):
        """The census must classify a market exactly as the POLY-002 matcher
        would, so `comparable_supply` predicts the matcher's own view rather than
        a second, divergent opinion."""
        from app.services.cross_venue import normalize_outcome as matcher_outcome

        poly_row(session, "p1", "Switzerland vs. Colombia: Over 2.5 Goals")
        session.commit()

        r = PolymarketCoverageReportService().build(session)
        expected = matcher_outcome("Switzerland vs. Colombia: Over 2.5 Goals")
        assert r.polymarket_market_types == {expected: 1}

    def test_two_sided_and_orderbook_coverage(self, session):
        poly_row(session, "p1", "Q1", two_sided=True, ob=True)
        poly_row(session, "p2", "Q2", two_sided=False, ob=True)
        session.commit()

        r = PolymarketCoverageReportService().build(session)
        assert r.two_sided_rate == 0.5
        assert r.orderbook_enabled == 2

    def test_uses_latest_snapshot_per_market(self, session):
        poly_row(session, "p1", "Q old", liq=1.0)
        poly_row(session, "p1", "Q new", liq=2.0)
        session.commit()

        r = PolymarketCoverageReportService().build(session)
        assert r.polymarket_markets == 1

    def test_empty_db_builds(self, session):
        r = PolymarketCoverageReportService().build(session)
        assert r.polymarket_markets == 0 and r.domains == []

    def test_kalshi_truncation_is_reported_not_silent(self, session):
        for i in range(3):
            kalshi(session, f"KXWC-{i}", "World Cup market")
        session.commit()

        capped = PolymarketCoverageReportService().build(session, kalshi_limit=2)
        assert capped.kalshi_markets == 2
        assert capped.kalshi_truncated is True

        full = PolymarketCoverageReportService().build(session, kalshi_limit=50)
        assert full.kalshi_markets == 3
        assert full.kalshi_truncated is False

    def test_cli_surfaces_kalshi_truncation(self, session, capsys):
        for i in range(3):
            kalshi(session, f"KXWC-{i}", "World Cup market")
        session.commit()

        asyncio.run(cli.polymarket_coverage_report(kalshi_limit=2, session=session))
        assert "TRUNCATED" in capsys.readouterr().out


# --- CLI ---------------------------------------------------------------------


class TestCLI:
    def test_scan_parses_new_options(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "polymarket_scan_once", fake)
        rc = cli.main([
            "polymarket-scan-once", "--limit", "300", "--orderbook-limit", "5",
            "--category", "21", "--include-closed", "--no-active-only",
            "--query", "world cup", "--query", "mlb", "--targeted",
            "--end-date-min", "2026-07-08T00:00:00Z", "--end-date-max", "2026-07-20T00:00:00Z",
        ])

        assert rc == 0
        assert captured["limit"] == 300
        assert captured["orderbook_limit"] == 5
        assert captured["category"] == 21
        assert captured["include_closed"] is True
        assert captured["active_only"] is False
        assert captured["query"] == ["world cup", "mlb"]
        assert captured["targeted"] is True
        assert captured["end_date_min"] == "2026-07-08T00:00:00Z"
        assert captured["end_date_max"] == "2026-07-20T00:00:00Z"

    def test_scan_defaults_are_conservative(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "polymarket_scan_once", fake)
        cli.main(["polymarket-scan-once"])

        assert captured["targeted"] is False
        assert captured["include_closed"] is False
        assert captured["active_only"] is True
        assert captured["query"] is None
        assert captured["category"] is None

    def test_scheduled_still_gated_by_flag(self, monkeypatch, session):
        s = Settings(_env_file=None, enable_polymarket_scout=False)
        monkeypatch.setattr(config_module, "get_settings", lambda: s)
        n = asyncio.run(cli.polymarket_scan_once(scheduled=True, session=session))
        assert n == 0
        assert session.query(PolymarketScoutRun).count() == 0

    def test_manual_scan_allowed_with_flag_off(self, monkeypatch, session):
        s = Settings(_env_file=None, enable_polymarket_scout=False)
        monkeypatch.setattr(config_module, "get_settings", lambda: s)

        from app.services.polymarket import PolymarketScoutRunner

        runner = PolymarketScoutRunner(scout=service_with(PagedAdapter(pages=[[market("m1")]])))
        n = asyncio.run(cli.polymarket_scan_once(limit=5, runner=runner, session=session))
        assert n == 1

    def test_coverage_report_cli_runs(self, session, capsys):
        poly_row(session, "p1", "Will France win the 2026 FIFA World Cup?")
        kalshi(session, "KXWC-1", "France to win the 2026 FIFA World Cup")
        session.commit()

        n = asyncio.run(cli.polymarket_coverage_report(session=session))
        out = capsys.readouterr().out
        assert n == 1
        assert "coverage report" in out
        assert "comparable_supply" in out


# --- safety ------------------------------------------------------------------

FORBIDDEN_SUBSTRINGS = (
    "expected_value", "kelly", "position_siz", "paper_trad", "place_order",
    "submit_order", "create_order", "wallet", "private_key", "keypair",
    "recommended_side", "trade_recommend", "execute_trade", "send_transaction",
    "swap", "jupiter", "sign_transaction",
)


class TestSafety:
    def test_no_forbidden_surface_in_coverage_modules(self):
        for module in (
            REPO / "app" / "services" / "polymarket_coverage.py",
            REPO / "app" / "adapters" / "polymarket.py",
            REPO / "app" / "services" / "polymarket.py",
        ):
            src = module.read_text().lower()
            # strip the boundary-statement docstrings/comments, which legitimately
            # name the forbidden capabilities in order to disclaim them
            code = "\n".join(
                line for line in src.splitlines()
                if not line.strip().startswith("#")
            )
            for bad in ("def place_order", "def submit_order", "def create_order",
                        "def sign", "private_key", "keypair", "def swap"):
                assert bad not in code, f"{module.name} exposes {bad!r}"

    def test_coverage_report_emits_no_arbitrage_or_ev_vocabulary(self, session):
        kalshi(session, "KXWC-1", "France to win the 2026 FIFA World Cup")
        poly_row(session, "p1", "Will France win the 2026 FIFA World Cup?")
        session.commit()

        r = PolymarketCoverageReportService().build(session)
        blob = " ".join([
            r.note, str(r.domains), str(r.comparable_supply_domains),
            str(r.no_comparable_supply_domains), str(r.polymarket_market_types),
        ]).lower()
        for term in ("arbitrage", "expected_value", "trade_candidate",
                     "recommend", "position_siz", "profit", "pnl"):
            assert term not in blob.replace("not arbitrage", "").replace(
                "not a recommendation", "").replace("not a trade candidate", "")

    def test_coverage_report_has_no_price_difference_field(self):
        from app.services.polymarket_coverage import DomainCoverage, PolymarketCoverageReport

        for cls in (DomainCoverage, PolymarketCoverageReport):
            fields = set(getattr(cls, "__annotations__", {}))
            for bad in ("observed_difference", "midpoint_difference", "edge", "ev",
                        "side", "size", "profit", "action", "arbitrage"):
                assert bad not in fields, f"{cls.__name__} exposes {bad!r}"

    def test_scout_run_model_has_no_forbidden_columns(self):
        cols = set(PolymarketScoutRun.__table__.columns.keys())
        for bad in ("side", "size", "ev", "expected_value", "profit", "wallet",
                    "order", "arbitrage", "arb", "recommendation"):
            assert bad not in cols

    def test_adapter_uses_only_public_no_auth_endpoints(self):
        src = (REPO / "app" / "adapters" / "polymarket.py").read_text()
        assert "gamma-api.polymarket.com" in src
        assert "clob.polymarket.com" in src
        for authed in ("/order", "/auth", "api_key", "Authorization",
                       "l1_header", "l2_header", "signature"):
            assert authed not in src, f"authenticated surface {authed!r} present"

    def test_search_and_catalog_are_get_only(self):
        src = (REPO / "app" / "adapters" / "polymarket.py").read_text()
        for verb in ("client.post", "client.put", "client.delete", "client.patch"):
            assert verb not in src

    def test_no_live_network_in_this_module(self):
        """The fake adapters override `_get` to raise. Call it through the
        instance (NOT `PolymarketAdapter._get(a, ...)`, which would bypass the
        override and perform a real request)."""
        a = PagedAdapter()
        with pytest.raises(AssertionError):
            asyncio.run(a._get(poly_adapter.GAMMA_API_BASE, "/markets"))


# --- migration up/down -------------------------------------------------------

NEW_COLUMNS = {"scan_mode", "pages_fetched", "market_fetch_errors",
               "duplicates_dropped", "queries_used"}


def test_migration_0022_round_trips(tmp_path):
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{tmp_path}/t.db"
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "0022")
    cols = {c["name"] for c in inspect(create_engine(url)).get_columns("polymarket_scout_runs")}
    assert NEW_COLUMNS <= cols

    command.downgrade(cfg, "0021")
    cols = {c["name"] for c in inspect(create_engine(url)).get_columns("polymarket_scout_runs")}
    assert not (NEW_COLUMNS & cols)

    command.upgrade(cfg, "0022")  # Column objects must not be reused across runs
    cols = {c["name"] for c in inspect(create_engine(url)).get_columns("polymarket_scout_runs")}
    assert NEW_COLUMNS <= cols


def test_migration_0022_is_orm_parity():
    model_cols = set(PolymarketScoutRun.__table__.columns.keys())
    assert NEW_COLUMNS <= model_cols
