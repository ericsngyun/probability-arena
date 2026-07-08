"""POLY-002 (precision-hardened by POLY-PRECISION-001): read-only Kalshi <->
Polymarket cross-venue OBSERVATION.

Identifies COMPARABLE markets across the two venues by deterministic semantic
matching (title/outcome/resolution normalization) and MEASURES observable
differences (midpoints on a 0..1 probability scale, spreads, liquidity proxies).

POLY-PRECISION-001 fixes two correctness defects found by POLY-COVERAGE-001 and
adds the gates that stop token overlap from being mistaken for identity:

  * SIDE ALIGNMENT. A Polymarket market's book (`best_bid`/`best_ask`) and
    `outcome_prices[0]` price `outcomes[0]`, which in ~26% of markets is a TEAM
    NAME, not "Yes". Kalshi, meanwhile, encodes the Yes side of a game market in
    the TICKER SUFFIX, not the title (`...SDLAD-SD` and `...-LAD` share the title
    "San Diego vs Los Angeles D Winner?"). The old code compared P(outcomes[0])
    to Kalshi's P(Yes) regardless. A midpoint — and therefore any
    `observed_difference` — is now produced ONLY when the Polymarket outcome has
    been explicitly aligned to the Kalshi YES proposition; otherwise both are
    absent and the pair is annotated `outcome_side_uncertain` /
    `midpoint_side_uncertain` and left `unresolved_semantic_match`.
  * OUTCOME NORMALIZATION. Over/under, handicap and scoreline signals live in
    punctuation that title normalization destroys, so "O/U 2.5" used to classify
    as `yes_no`. They are now read from the raw title, before stripping.

Plus deterministic compatibility gates — outcome type, market scope (player prop
vs game vs tournament future), over/under and handicap thresholds, entity-anchored
scorelines, named-entity overlap and sport identity — so a Counter-Strike map
market can no longer match a Valorant one on the shared tokens "map/2/winner".

Hard boundary (docs/SAFETY_BOUNDARIES.md): this is observation and measurement
only. It does NOT compute EV, label arbitrage/"arb", identify trades, recommend
a side, size a position, place/cancel orders, paper trade, or touch
wallets/keys/swaps/signing/execution. A `match_label` is a semantic-comparability
verdict for human review; an `observed_difference` is a measured probability gap
between two venues' quotes — never a signal, a return, or an action.
`large_observed_difference_requires_review` is a REVIEW flag on a suspicious
match, never an opportunity. All inputs are already-persisted rows (Kalshi
markets/snapshots + POLY-001 polymarket markets); no external call is made here.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CrossVenueMarketCandidate,
    CrossVenueObservationRun,
    Market,
    MarketSnapshot,
    PolymarketMarket,
)

logger = logging.getLogger(__name__)

# --- match labels (semantic comparability verdicts for human review) --------
LABEL_COMPARABLE = "comparable_market_candidate"
LABEL_UNRESOLVED = "unresolved_semantic_match"
LABEL_INCOMPATIBLE_OUTCOME = "incompatible_outcome"
LABEL_INCOMPATIBLE_RESOLUTION = "incompatible_resolution"
LABEL_LOW_CONFIDENCE = "low_confidence_match"
MATCH_LABELS = (
    LABEL_COMPARABLE, LABEL_LOW_CONFIDENCE, LABEL_UNRESOLVED,
    LABEL_INCOMPATIBLE_OUTCOME, LABEL_INCOMPATIBLE_RESOLUTION,
)

# --- mismatch / suspicion reasons (POLY-PRECISION-001) ----------------------
# Each is a REVIEW annotation explaining why two markets are not (or may not be)
# describing the same proposition. None is an opportunity, a signal, or an action.
REASON_MARKET_TYPE_MISMATCH = "market_type_mismatch"
REASON_THRESHOLD_MISMATCH = "threshold_mismatch"
REASON_ENTITY_MISMATCH = "entity_mismatch"
REASON_SPORT_OR_GAME_MISMATCH = "sport_or_game_mismatch"
REASON_OUTCOME_SIDE_UNCERTAIN = "outcome_side_uncertain"
REASON_MIDPOINT_SIDE_UNCERTAIN = "midpoint_side_uncertain"
REASON_LARGE_DIFFERENCE = "large_observed_difference_requires_review"

# thresholds (conservative + deterministic)
MIN_TITLE_SIM_FLOOR = 0.2      # below this there is no plausible comparable at all
HIGH_SIM = 0.45
LOW_SIM = 0.30
RESOLUTION_PROXIMATE_DAYS = 3
RESOLUTION_MAX_DAYS = 10       # beyond this the two markets resolve on different events
# A large measured gap is never grounds for rejection on its own (two venues may
# genuinely disagree, or one quote may be stale). It is grounds for a REVIEW flag,
# because in practice a large gap between supposedly identical propositions most
# often means the match itself is wrong. Never an opportunity signal.
LARGE_OBSERVED_DIFFERENCE = 0.35
HIGH_SEMANTIC_CONFIDENCE = 0.85

DISCLAIMER = (
    "Read-only cross-venue OBSERVATION. `match_label` is a semantic-comparability "
    "verdict for human review and `observed_difference` is a measured probability "
    "gap between the two venues' midpoints — NOT arbitrage, NOT EV, NOT a trade, "
    "NOT a side, NOT a size, NOT a recommendation, NOT an action. No dollars, "
    "profit, orders, wallets, keys, swaps, signing, or execution."
)

_STOPWORDS = frozenset({
    "will", "the", "a", "an", "to", "at", "on", "in", "of", "for", "be", "by",
    "and", "or", "is", "are", "this", "that", "who", "what", "which", "market",
    "vs", "versus", "v",
})
_OUTCOME_YES_NO = "yes_no"
_OUTCOME_WINNER = "winner"
_OUTCOME_OVER_UNDER = "over_under"
_OUTCOME_SPREAD = "spread"
_OUTCOME_ADVANCE = "advance"
_OUTCOME_CANDIDATE = "candidate_winner"
_OUTCOME_EVENT = "event_outcome"
_OUTCOME_EXACT_SCORE = "exact_score"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- deterministic semantic normalizer --------------------------------------


def normalize_title(text: str | None) -> str:
    """Lowercase, strip punctuation, normalize vs/versus + whitespace. Pure."""
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"\bvs?\.?\b|\bversus\b", " vs ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def title_tokens(text: str | None) -> frozenset[str]:
    return frozenset(w for w in normalize_title(text).split() if w and w not in _STOPWORDS)


# Over/under and handicap signals live in punctuation that `normalize_title`
# destroys ("O/U 2.5" -> "o u 2 5"; "(-1.5)" -> " 1 5"). They must be detected on
# the RAW lowercased title, before stripping. (POLY-PRECISION-001: previously
# "O/U 2.5" fell through to `yes_no`, and the `[+-]\d` spread test could never
# fire because the sign had already been removed.)
_OU_RE = re.compile(r"\bo\s*/\s*u\b|\bover\b|\bunder\b|\btotals?\b")
# A sign only means a handicap line when it opens a token — otherwise "GPT-5.6"
# and "2026-07-09" would both read as spreads.
_SIGNED_LINE_RE = re.compile(r"(?:^|[\s(\[])[+-]\s?\d")
_HANDICAP_RE = re.compile(r"\bspread\b|\bhandicap\b|\bby (?:more|fewer|less) than\b")

# A scoreline ("Spain 1 - 3 Belgium", "Portugal wins 4-3") is NOT a handicap: the
# hyphen separates two scores. It must be removed before any signed-line test,
# or "Exact Score: Spain 1 - 3 Belgium?" reads as a spread of -3.
_SCORE_PAIR_RE = re.compile(r"\d\s*[-–]\s*\d")
_EXACT_SCORE_RE = re.compile(r"\bexact score\b|\bfinal score\b")
# "<Entity> wins A-B"  /  "<TeamA> A - B <TeamB>" — the entity named first owns
# the first score, which is what makes two scorelines comparable across venues.
_SCORE_WINS_RE = re.compile(
    r"([A-Z][\w'’.\-]*(?:\s+[A-Z][\w'’.\-]*)*)\s+wins?\s+(\d+)\s*[-–]\s*(\d+)"
)
_SCORE_PAIR_ENTITIES_RE = re.compile(
    r"([A-Z][\w'’.\-]*(?:\s+[A-Z][\w'’.\-]*)*)\s+(\d+)\s*[-–]\s*(\d+)\s+([A-Z][\w'’.\-]*(?:\s+[A-Z][\w'’.\-]*)*)"
)


def _without_scorelines(raw: str) -> str:
    return _SCORE_PAIR_RE.sub("  ", raw)


def score_by_entity(text: str | None) -> dict[str, int]:
    """Map each named entity to the score attributed to it in an exact-score
    title. "Spain wins 3-1" -> {spain: 3}; "Spain 1 - 3 Belgium" -> {spain: 1,
    belgium: 3}. Empty when no scoreline is present.

    Anchoring on the ENTITY (not on position) is what lets two venues' scorelines
    be compared: Kalshi writes "Spain wins 3-1" where Polymarket writes
    "Spain 1 - 3 Belgium" — the same Spain, contradictory scores."""
    if not text:
        return {}
    scores: dict[str, int] = {}
    m = _SCORE_PAIR_ENTITIES_RE.search(text)
    if m:
        first, a, b, second = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        for name, value in ((first, a), (second, b)):
            for token in title_tokens(name):
                scores.setdefault(token, value)
        return scores
    m = _SCORE_WINS_RE.search(text)
    if m:
        for token in title_tokens(m.group(1)):
            scores.setdefault(token, int(m.group(2)))
    return scores


_THRESHOLD_RE = re.compile(
    r"(?:over|under|o\s*/\s*u|totals?)\D{0,16}?(\d+(?:\.\d+)?)"
)
_ANY_HALF_LINE_RE = re.compile(r"\b(\d+\.\d+)\b")
_SPREAD_LINE_RE = re.compile(r"(?:^|[\s(\[])([+-]\s?\d+(?:\.\d+)?)")


def normalize_outcome(text: str | None) -> str:
    """Map a market title/outcome to a canonical outcome TYPE (conservative).

    Detection order matters: over/under and handicap lines are read from the RAW
    text so that punctuation-carried signals ("O/U", "(-1.5)") survive."""
    raw = (text or "").lower()
    t = normalize_title(text)
    if not t:
        return _OUTCOME_EVENT
    if "advance" in t:
        return _OUTCOME_ADVANCE
    # exact-score before over/under and handicap: "Spain 1 - 3 Belgium" is a
    # scoreline, not a -3 line, and "Spain wins 3-1" is not a winner market.
    if _EXACT_SCORE_RE.search(raw) or _SCORE_PAIR_ENTITIES_RE.search(text or ""):
        return _OUTCOME_EXACT_SCORE
    if _OU_RE.search(raw):
        return _OUTCOME_OVER_UNDER
    descored = _without_scorelines(raw)
    if _HANDICAP_RE.search(descored) or _SIGNED_LINE_RE.search(descored):
        return _OUTCOME_SPREAD
    if any(w in t for w in ("election", "elected", "president", "nominee", "leader", "power")):
        return _OUTCOME_CANDIDATE
    if any(w in t for w in ("win", "winner", "champion")):
        return _OUTCOME_WINNER
    return _OUTCOME_YES_NO


def refine_outcome_type(base_type: str, outcomes: list | None) -> str:
    """Refine a title-derived outcome type using the market's OUTCOME LABELS.

    A Polymarket market titled "Kansas City Royals vs. New York Mets" names no
    verb, so the title alone reads `yes_no` — but its outcomes are the two teams,
    which makes it a match-winner market. Likewise ["Over","Under"] outcomes make
    a market an over/under whatever the title says. Kalshi rows carry no outcome
    labels and are unaffected."""
    labels = [str(o).strip().lower() for o in (outcomes or []) if str(o).strip()]
    if len(labels) != 2:
        return base_type
    distinct = set(labels)
    if distinct == {"over", "under"}:
        return _OUTCOME_OVER_UNDER
    if distinct == {"yes", "no"}:
        return base_type
    if base_type in (_OUTCOME_YES_NO, _OUTCOME_EVENT):
        return _OUTCOME_WINNER  # two named contenders => the question is who wins
    return base_type


def extract_threshold(text: str | None) -> float | None:
    """The over/under line ("O/U 2.5" -> 2.5, "Will over 5.5 goals" -> 5.5).
    None when no line can be parsed — never guessed."""
    raw = (text or "").lower()
    m = _THRESHOLD_RE.search(raw)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    m = _ANY_HALF_LINE_RE.search(raw)
    if m and _OU_RE.search(raw):
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def extract_spread_line(text: str | None) -> float | None:
    """The handicap line ("Spread: Colombia (-1.5)" -> -1.5, "wins by more than
    2.5 goals" -> 2.5). Magnitude-comparable; None when unparseable. Scorelines
    are removed first so "Spain 1 - 3 Belgium" never yields -3."""
    raw = _without_scorelines((text or "").lower())
    m = _SPREAD_LINE_RE.search(raw)
    if m:
        try:
            return float(m.group(1).replace(" ", ""))
        except ValueError:
            return None
    if _HANDICAP_RE.search(raw):
        m = _ANY_HALF_LINE_RE.search(raw)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


# outcome types that describe the SAME kind of yes-probability question and can
# therefore be READ on a shared 0..1 midpoint scale. This is a scale statement,
# not a compatibility statement — see `_OUTCOME_COMPATIBILITY`.
_YESISH = frozenset({
    _OUTCOME_YES_NO, _OUTCOME_WINNER, _OUTCOME_ADVANCE, _OUTCOME_CANDIDATE,
    _OUTCOME_EXACT_SCORE,
})

# Which outcome types may describe the same proposition. POLY-PRECISION-001
# tightened this: previously every yes-ish type matched every other yes-ish type,
# so a yes/no prop could be declared comparable to a tournament winner future.
_OUTCOME_COMPATIBILITY: dict[str, frozenset[str]] = {
    _OUTCOME_YES_NO: frozenset({_OUTCOME_YES_NO, _OUTCOME_EVENT}),
    _OUTCOME_EVENT: frozenset({_OUTCOME_EVENT, _OUTCOME_YES_NO}),
    _OUTCOME_WINNER: frozenset({_OUTCOME_WINNER, _OUTCOME_CANDIDATE}),
    _OUTCOME_CANDIDATE: frozenset({_OUTCOME_CANDIDATE, _OUTCOME_WINNER}),
    _OUTCOME_ADVANCE: frozenset({_OUTCOME_ADVANCE}),
    _OUTCOME_OVER_UNDER: frozenset({_OUTCOME_OVER_UNDER}),
    _OUTCOME_SPREAD: frozenset({_OUTCOME_SPREAD}),
    _OUTCOME_EXACT_SCORE: frozenset({_OUTCOME_EXACT_SCORE}),
}


def outcome_is_yes_scale(outcome_type: str) -> bool:
    """True when the outcome type is a yes-probability question readable on a
    shared 0..1 midpoint scale. Exposed so coverage reporting can reuse the
    matcher's own vocabulary rather than duplicating it."""
    return outcome_type in _YESISH


def outcomes_compatible(a: str, b: str) -> bool:
    """True only when the two outcome types can describe the same proposition."""
    return b in _OUTCOME_COMPATIBILITY.get(a, frozenset({a}))


# --- market scope: props vs game vs tournament future ------------------------
SCOPE_PLAYER_PROP = "player_prop"
SCOPE_GAME = "game"
SCOPE_TOURNAMENT_FUTURE = "tournament_future"
SCOPE_EVENT = "event"  # unknown/other — treated as a wildcard, never a rejection

_PLAYER_PROP_RE = re.compile(
    r"\b\d+\+|\bdrafted\b|\bplayer to\b|\bstrikeouts?\b|\bhome runs?\b|\brbis?\b"
    r"|\bassists?\b|\brebounds?\b|\bstolen bases?\b|\bbatting average\b"
)
_TOURNAMENT_RE = re.compile(
    r"\bworld cup\b|\bworld series\b|\bchampionship\b|\bchampion\b|\btrophy\b"
    r"|\bmvp\b|\baward\b|\bgolden (?:ball|boot|glove)\b|\bleader\b|\bpostseason\b"
    r"|\bdraft\b|\bfinals\b"
)
_GAME_RE = re.compile(r"\bvs\.?\b|\bversus\b|\bmap \d")


def market_scope(text: str | None) -> str:
    """Coarse scope of what a market resolves on. Conservative: unknown -> event,
    which never causes a rejection on its own."""
    raw = (text or "").lower()
    if not raw:
        return SCOPE_EVENT
    if _PLAYER_PROP_RE.search(raw):
        return SCOPE_PLAYER_PROP
    if _GAME_RE.search(raw):
        return SCOPE_GAME
    if _TOURNAMENT_RE.search(raw):
        return SCOPE_TOURNAMENT_FUTURE
    return SCOPE_EVENT


def scopes_compatible(a: str, b: str) -> bool:
    """A player prop never resolves on the same event as a game or a tournament
    future, and a single game never resolves a tournament future. `event` is
    unknown, so it is compatible with everything (we do not reject on ignorance)."""
    if a == b or SCOPE_EVENT in (a, b):
        return True
    return False


# --- sport / game identity ---------------------------------------------------
# Prevents "Counter-Strike 2 map 2 winner" from matching "Valorant map 2 winner"
# purely on token overlap ("map", "2", "winner", "esports").
_SPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cs2", ("cs2", "counter strike", "counter-strike", "csgo", "cs go")),
    ("valorant", ("valorant",)),
    ("dota", ("dota",)),
    ("league_of_legends", ("league of legends",)),
    ("baseball", ("mlb", "baseball", "world series")),
    ("soccer", ("world cup", "fifa", "soccer", "premier league", "uefa", "la liga", "bundesliga")),
    ("tennis", ("tennis", "atp", "wta", "itf", "wimbledon")),
    ("basketball", ("nba", "basketball")),
    ("american_football", ("nfl", "super bowl")),
)
_TICKER_SPORTS: tuple[tuple[str, str], ...] = (
    ("KXCS2", "cs2"), ("KXVAL", "valorant"), ("KXDOTA", "dota"),
    ("KXLOL", "league_of_legends"), ("KXMLB", "baseball"), ("KXWC", "soccer"),
    ("KXITF", "tennis"), ("KXATP", "tennis"), ("KXWTA", "tennis"),
    ("KXNBA", "basketball"), ("KXNFL", "american_football"),
)


def detect_sport(text: str | None, ticker: str | None = None) -> str | None:
    """The sport/title a market belongs to, or None when it cannot be determined
    unambiguously. A Kalshi ticker prefix is authoritative; otherwise the title
    must match exactly one sport (ambiguity -> None -> no rejection)."""
    if ticker:
        upper = ticker.upper()
        for prefix, sport in _TICKER_SPORTS:
            if upper.startswith(prefix):
                return sport
    raw = (text or "").lower()
    hits = {sport for sport, terms in _SPORTS if any(term in raw for term in terms)}
    return next(iter(hits)) if len(hits) == 1 else None


# --- entity identity ---------------------------------------------------------
# Team/player names are Capitalized in both venues' titles, so capitalization is
# the signal. Generic words are dropped so that "Esports"/"Gaming"/"Winner" can
# never be the sole evidence that two markets share an entity.
_GENERIC_ENTITY_WORDS = frozenset({
    "will", "who", "what", "which", "the", "a", "an", "is", "are", "be",
    "map", "winner", "win", "wins", "total", "totals", "over", "under",
    "spread", "yes", "no", "esports", "gaming", "team", "league", "cup",
    "world", "series", "game", "match", "final", "finals", "draft", "fc",
    "vs", "versus", "score", "goals", "player", "top",
})
_CAPITALIZED_RE = re.compile(r"\b[A-Z][A-Za-z0-9'’.\-]*")


def entity_tokens(text: str | None) -> frozenset[str]:
    """Proper-noun-ish entity tokens from the RAW title (capitalization matters).
    Empty for all-caps titles, where capitalization carries no signal."""
    if not text:
        return frozenset()
    letters = [c for c in text if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        return frozenset()
    out = set()
    for word in _CAPITALIZED_RE.findall(text):
        cleaned = re.sub(r"[^a-z0-9]", "", word.lower())
        if len(cleaned) >= 2 and cleaned not in _GENERIC_ENTITY_WORDS:
            out.add(cleaned)
    return frozenset(out)


# --- Kalshi yes-side identity ------------------------------------------------
# Kalshi encodes the Yes side of a game market in the TICKER SUFFIX, not the
# title: `KXMLBGAME-...SDLAD-SD` and `...-LAD` share the title "San Diego vs Los
# Angeles D Winner?". When the title does not name the Yes entity, the side is
# genuinely unknowable from persisted data and must not be guessed.
_YES_ENTITY_PATTERNS = (
    re.compile(r"^\s*will\s+(?:the\s+)?(.+?)\s+win\b", re.IGNORECASE),
    re.compile(r"^\s*([A-Z][\w'’.\-]*(?:\s+[A-Z][\w'’.\-]*)*)\s+wins?\b"),
)


def extract_kalshi_yes_entity(title: str | None) -> str | None:
    """The entity a Kalshi market's YES outcome refers to, or None when the title
    does not identify it (e.g. "San Diego vs Los Angeles D Winner?")."""
    if not title:
        return None
    for pattern in _YES_ENTITY_PATTERNS:
        m = pattern.search(title)
        if m:
            candidate = m.group(1).strip()
            if candidate and title_tokens(candidate):
                return candidate
    return None


def resolve_poly_yes_index(
    outcomes: list | None, kalshi_title: str | None
) -> tuple[int | None, str | None]:
    """Which Polymarket outcome corresponds to the Kalshi market's YES side.

    Returns (index, None) when the side is certain, or (None, reason) when it is
    not. Polymarket's market-level `best_bid`/`best_ask` and `outcome_prices[0]`
    describe `outcomes[0]`, which is frequently a TEAM NAME rather than "Yes"
    (~26% of markets observed). Comparing that to Kalshi's P(Yes) without an
    explicit side mapping is what produced the POLY-COVERAGE-001 false positives.
    """
    labels = [str(o).strip().lower() for o in (outcomes or []) if str(o).strip()]
    if not labels:
        return None, REASON_MIDPOINT_SIDE_UNCERTAIN  # no outcome metadata at all
    if len(labels) != 2:
        return None, REASON_OUTCOME_SIDE_UNCERTAIN   # not a binary proposition

    distinct = set(labels)
    if distinct == {"yes", "no"}:
        return labels.index("yes"), None
    if distinct == {"over", "under"}:
        raw = (kalshi_title or "").lower()
        over, under = bool(re.search(r"\bover\b", raw)), bool(re.search(r"\bunder\b", raw))
        if over and not under:
            return labels.index("over"), None
        if under and not over:
            return labels.index("under"), None
        return None, REASON_OUTCOME_SIDE_UNCERTAIN

    entity = extract_kalshi_yes_entity(kalshi_title)
    if not entity:
        return None, REASON_OUTCOME_SIDE_UNCERTAIN
    wanted = title_tokens(entity)
    matches = [i for i, label in enumerate(labels) if title_tokens(label) & wanted]
    if len(matches) == 1:
        return matches[0], None
    return None, REASON_OUTCOME_SIDE_UNCERTAIN


def coarse_domain(*texts: str | None) -> str:
    """Coarse topic bucket used to gate matching. Conservative: unknown -> other."""
    blob = " ".join(normalize_title(t) for t in texts if t)
    if any(w in blob for w in (
        "world cup", "wimbledon", "tennis", "nba", "nfl", "mlb", "soccer",
        "football", "game", "match", "champion", "vs", "corners", "goals",
    )):
        return "sports"
    if any(w in blob for w in (
        "election", "president", "nominee", "leader", "power", "senate",
        "congress", "governor", "prime minister", "party",
    )):
        return "politics"
    if any(w in blob for w in ("bitcoin", "ethereum", "eth", "btc", "crypto", "solana")):
        return "crypto"
    if any(w in blob for w in ("gdp", "inflation", "cpi", "fed", "rate", "unemployment")):
        return "economics"
    return "other"


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return round(len(a & b) / len(a | b), 4)


# --- venue view dataclasses -------------------------------------------------


@dataclass
class KalshiView:
    ticker: str
    event_ticker: str | None
    title: str
    domain: str
    outcome_type: str
    tokens: frozenset[str]
    resolution_time: datetime | None
    midpoint: float | None          # P(Yes) on a 0..1 scale
    spread: float | None
    liquidity_proxy: float | None
    snapshot_age_seconds: float | None
    scope: str = SCOPE_EVENT
    sport: str | None = None
    entities: frozenset[str] = frozenset()
    threshold: float | None = None
    spread_line: float | None = None
    scoreline: dict = field(default_factory=dict)  # entity token -> its score


@dataclass
class PolyView:
    market_id: str
    condition_id: str | None
    token_id: str | None
    question: str
    domain: str
    outcome_type: str
    tokens: frozenset[str]
    resolution_time: datetime | None
    spread: float | None
    liquidity_proxy: float | None
    # Side-bearing price inputs. There is deliberately NO precomputed `midpoint`:
    # a Polymarket midpoint is only meaningful once the outcome side has been
    # aligned to the Kalshi market's YES proposition.
    outcomes: list = field(default_factory=list)
    outcome_prices: list = field(default_factory=list)
    best_bid: float | None = None
    best_ask: float | None = None
    scope: str = SCOPE_EVENT
    sport: str | None = None
    entities: frozenset[str] = frozenset()
    threshold: float | None = None
    spread_line: float | None = None
    scoreline: dict = field(default_factory=dict)  # entity token -> its score

    def probability_for_index(self, index: int) -> float | None:
        """P(outcomes[index]) on a 0..1 scale, or None when no price exists.

        Polymarket's market-level book (`best_bid`/`best_ask`) quotes `outcomes[0]`
        — verified against the live API: the book midpoint equals
        `outcome_prices[0]` in 97/97 sampled markets that had a book. For a binary
        market the complement is the other side."""
        if self.best_bid is not None and self.best_ask is not None:
            first = (self.best_bid + self.best_ask) / 2
            return round(first if index == 0 else 1.0 - first, 4)
        if isinstance(self.outcome_prices, list) and len(self.outcome_prices) > index:
            try:
                return round(float(self.outcome_prices[index]), 4)
            except (TypeError, ValueError):
                return None
        return None


def _kalshi_midpoint(snap: MarketSnapshot) -> tuple[float | None, float | None]:
    """(midpoint, spread) on a 0..1 probability scale from cents. None when the
    quote is one-sided/absent (no fabricated price)."""
    if snap.yes_bid is not None and snap.yes_ask is not None:
        return round((snap.yes_bid + snap.yes_ask) / 200, 4), round((snap.yes_ask - snap.yes_bid) / 100, 4)
    if snap.last_price is not None:
        return round(snap.last_price / 100, 4), None
    return None, None


def poly_yes_midpoint(p: "PolyView", k: "KalshiView") -> tuple[float | None, str | None]:
    """The Polymarket probability of the SAME proposition the Kalshi market's YES
    side describes, or (None, reason) when the side cannot be aligned.

    Never falls back to `outcome_prices[0]`: that is the probability of whichever
    outcome happens to be listed first, which for a game market is an arbitrary
    team."""
    index, reason = resolve_poly_yes_index(p.outcomes, k.title)
    if index is None:
        return None, reason
    price = p.probability_for_index(index)
    if price is None:
        return None, REASON_MIDPOINT_SIDE_UNCERTAIN
    return price, None


@dataclass
class MatchSampleComposition:
    """Read-only description of WHICH persisted rows a match pass considered, so a
    manual operator can see the sample rather than guess at limits. Pure
    diagnostics — no scores, no advice, no action. Attached transiently to the
    run (never persisted; no DB column)."""

    kalshi_loaded: int = 0
    polymarket_loaded: int = 0
    kalshi_considered: int = 0
    polymarket_considered: int = 0
    kalshi_load_mode: str = "recent_active"
    recent_hours: int | None = None
    domain_filter: str | None = None
    market_type_filter: str | None = None
    kalshi_stale_skipped: int = 0       # active but outside the recency window
    kalshi_without_snapshot: int = 0    # unavailable: no snapshot => no midpoint
    kalshi_by_domain: dict = field(default_factory=dict)
    polymarket_by_domain: dict = field(default_factory=dict)
    kalshi_by_market_type: dict = field(default_factory=dict)
    polymarket_by_market_type: dict = field(default_factory=dict)
    overlap_domains: list = field(default_factory=list)
    low_overlap: bool = False


def _count_by(views, key) -> dict:
    counts: dict[str, int] = {}
    for v in views:
        counts[key(v)] = counts.get(key(v), 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


class CrossVenueMatchingService:
    """Deterministic Kalshi<->Polymarket matcher + observer over persisted rows."""

    def _load_polymarket(self, session: Session, limit: int) -> list[PolyView]:
        rows = session.execute(
            select(PolymarketMarket).order_by(PolymarketMarket.id.desc())
        ).scalars().all()
        latest: dict[str, PolymarketMarket] = {}
        for m in rows:
            latest.setdefault(m.market_id, m)
        views: list[PolyView] = []
        for m in list(latest.values())[:limit]:
            token_id = None
            if isinstance(m.clob_token_ids, list) and m.clob_token_ids:
                token_id = str(m.clob_token_ids[0])
            question = m.question or ""
            outcomes = list(m.outcomes) if isinstance(m.outcomes, list) else []
            views.append(PolyView(
                market_id=m.market_id, condition_id=m.condition_id, token_id=token_id,
                question=question, domain=coarse_domain(m.question, m.category),
                outcome_type=refine_outcome_type(normalize_outcome(question), outcomes),
                tokens=title_tokens(question),
                resolution_time=_aware(m.end_date),
                spread=(round(m.spread, 4) if m.spread is not None else None),
                liquidity_proxy=m.liquidity_usd,
                outcomes=list(m.outcomes) if isinstance(m.outcomes, list) else [],
                outcome_prices=list(m.outcome_prices) if isinstance(m.outcome_prices, list) else [],
                best_bid=m.best_bid, best_ask=m.best_ask,
                scope=market_scope(question),
                sport=detect_sport(f"{question} {m.category or ''}"),
                entities=entity_tokens(question),
                threshold=extract_threshold(question),
                spread_line=extract_spread_line(question),
                scoreline=score_by_entity(question),
            ))
        return views

    def _load_kalshi(
        self, session: Session, limit: int, recent_hours: int | None = None
    ) -> tuple[list[KalshiView], str, int]:
        """Load a bounded, RECENCY-ORDERED slice of Kalshi markets.

        Returns (views, load_mode, stale_skipped). Ordering by `last_seen_at`
        descending is the whole point: the previous unordered `.limit()` returned
        markets in rowid order, i.e. the OLDEST-inserted markets first — which on
        a long-running DB are stale rows still flagged 'active' but not refreshed
        for days, with no overlap against a freshly-scanned Polymarket sample.
        This changes only WHICH rows are considered, never how they are matched.

        `recent_hours`, when set, additionally drops markets not seen inside that
        window (the cleanest way to exclude stale 'active' rows). Tiered fallback
        keeps the command working on unusual datasets: recency-window -> active
        (no window) -> any-status-by-recency."""
        now = _now()
        order = (Market.last_seen_at.desc(), Market.id.desc())
        stale_skipped = 0

        stmt = select(Market).where(Market.status == "active")
        if recent_hours and recent_hours > 0:
            cutoff = now - timedelta(hours=recent_hours)
            stale_skipped = session.execute(
                select(func.count()).select_from(Market)
                .where(Market.status == "active", Market.last_seen_at < cutoff)
            ).scalar() or 0
            stmt = stmt.where(Market.last_seen_at >= cutoff)
        markets = session.execute(stmt.order_by(*order).limit(limit)).scalars().all()
        load_mode = "recent_active"

        if not markets and recent_hours:  # window emptied it — active, no window
            load_mode = "recent_active_no_window"
            markets = session.execute(
                select(Market).where(Market.status == "active")
                .order_by(*order).limit(limit)
            ).scalars().all()
        if not markets:  # some datasets use "open"/etc. — most-recently-seen, any status
            load_mode = "any_status_recent"
            markets = session.execute(
                select(Market).order_by(*order).limit(limit)
            ).scalars().all()

        views: list[KalshiView] = []
        for mk in markets:
            snap = session.execute(
                select(MarketSnapshot).where(MarketSnapshot.market_id == mk.id)
                .order_by(MarketSnapshot.id.desc()).limit(1)
            ).scalars().first()
            mid = spread = liq = age = None
            if snap is not None:
                mid, spread = _kalshi_midpoint(snap)
                liq = float(snap.liquidity) if snap.liquidity is not None else None
                if snap.captured_at is not None:
                    age = (now - _aware(snap.captured_at)).total_seconds()
            res = _aware(mk.close_time) or _aware(mk.expiration_time)
            title = mk.title or ""
            views.append(KalshiView(
                ticker=mk.ticker, event_ticker=mk.event_ticker, title=title,
                domain=coarse_domain(mk.title, mk.category, mk.ticker),
                outcome_type=normalize_outcome(title), tokens=title_tokens(title),
                resolution_time=res, midpoint=mid, spread=spread, liquidity_proxy=liq,
                snapshot_age_seconds=age,
                scope=market_scope(title),
                sport=detect_sport(f"{title} {mk.category or ''}", ticker=mk.ticker),
                entities=entity_tokens(title),
                threshold=extract_threshold(title),
                spread_line=extract_spread_line(title),
                scoreline=score_by_entity(title),
            ))
        return views, load_mode, stale_skipped

    @staticmethod
    def _is_hard_incompatible(p: PolyView, k: KalshiView) -> bool:
        """Short-circuiting boolean twin of `_hard_incompatibilities`, used in the
        O(polymarket x kalshi) match loop where formatting reason strings for
        millions of rejected pairs would dominate the runtime. Must stay in step
        with `_hard_incompatibilities` — a shared test asserts they agree."""
        if not outcomes_compatible(p.outcome_type, k.outcome_type):
            return True
        if not scopes_compatible(p.scope, k.scope):
            return True
        if p.sport and k.sport and p.sport != k.sport:
            return True
        if (
            p.outcome_type == _OUTCOME_OVER_UNDER == k.outcome_type
            and p.threshold is not None and k.threshold is not None
            and p.threshold != k.threshold
        ):
            return True
        if (
            p.outcome_type == _OUTCOME_SPREAD == k.outcome_type
            and p.spread_line is not None and k.spread_line is not None
            and abs(p.spread_line) != abs(k.spread_line)
        ):
            return True
        if p.outcome_type == _OUTCOME_EXACT_SCORE == k.outcome_type:
            shared = set(p.scoreline) & set(k.scoreline)
            if any(p.scoreline[e] != k.scoreline[e] for e in shared):
                return True
        if p.entities and k.entities and not (p.entities & k.entities):
            return True
        return False

    @staticmethod
    def _hard_incompatibilities(p: PolyView, k: KalshiView) -> list[str]:
        """Reasons the two markets CANNOT describe the same proposition. Each is a
        structural contradiction, not a price judgement. Deterministic and
        order-stable so the same pair always yields the same reasons."""
        reasons: list[str] = []

        if not outcomes_compatible(p.outcome_type, k.outcome_type):
            reasons.append(f"outcome_type_mismatch={p.outcome_type}!={k.outcome_type}")

        if not scopes_compatible(p.scope, k.scope):
            reasons.append(f"{REASON_MARKET_TYPE_MISMATCH}={p.scope}!={k.scope}")

        # A sport is only decided when both sides name one unambiguously.
        if p.sport and k.sport and p.sport != k.sport:
            reasons.append(f"{REASON_SPORT_OR_GAME_MISMATCH}={p.sport}!={k.sport}")

        # Lines only compare when BOTH parse; an unparseable line is uncertainty,
        # not a contradiction (it blocks `comparable` further down instead).
        if p.outcome_type == _OUTCOME_OVER_UNDER == k.outcome_type:
            if p.threshold is not None and k.threshold is not None and p.threshold != k.threshold:
                reasons.append(f"{REASON_THRESHOLD_MISMATCH}={p.threshold}!={k.threshold}")
        if p.outcome_type == _OUTCOME_SPREAD == k.outcome_type:
            if (
                p.spread_line is not None and k.spread_line is not None
                and abs(p.spread_line) != abs(k.spread_line)
            ):
                reasons.append(f"{REASON_THRESHOLD_MISMATCH}={p.spread_line}!={k.spread_line}")

        # Exact-score markets: anchor each score on the entity it belongs to and
        # compare. "Spain wins 3-1" and "Spain 1 - 3 Belgium" name the same Spain
        # with contradictory scores, so they are different propositions.
        if p.outcome_type == _OUTCOME_EXACT_SCORE == k.outcome_type:
            for entity in sorted(set(p.scoreline) & set(k.scoreline)):
                if p.scoreline[entity] != k.scoreline[entity]:
                    reasons.append(
                        f"{REASON_THRESHOLD_MISMATCH}={entity}:{p.scoreline[entity]}"
                        f"!={k.scoreline[entity]}"
                    )
                    break

        # Disjoint named entities: two markets that name teams/players and share
        # none of them are about different contests, however many generic tokens
        # ("map", "winner", "esports") they happen to share.
        if p.entities and k.entities and not (p.entities & k.entities):
            reasons.append(REASON_ENTITY_MISMATCH)

        return reasons

    def _best_match(self, p: PolyView, kalshi: list[KalshiView]) -> tuple[KalshiView | None, float]:
        """Highest token-similarity Kalshi market, PREFERRING one with no hard
        incompatibility. Token overlap alone happily pairs Counter-Strike with
        Valorant; preferring a compatible partner improves precision (the false
        pair stops winning) and recall (a real pair is no longer shadowed)."""
        best_ok, best_ok_sim = None, 0.0
        best_any, best_any_sim = None, 0.0
        for k in kalshi:
            # gate by coarse domain (allow 'other' on either side, at lower weight)
            if p.domain != k.domain and "other" not in (p.domain, k.domain):
                continue
            sim = jaccard(p.tokens, k.tokens)
            if p.domain == k.domain and p.domain != "other":
                sim = round(min(1.0, sim + 0.05), 4)  # small same-domain bonus
            if sim > best_any_sim:
                best_any, best_any_sim = k, sim
            if sim > best_ok_sim and not self._is_hard_incompatible(p, k):
                best_ok, best_ok_sim = k, sim
        # Only prefer the compatible partner when it is itself plausible. Otherwise
        # fall back to the best overall pair so its REJECTION is still recorded —
        # a compatible-but-implausible partner would be dropped by the similarity
        # floor, silently discarding the audit row for the real near-match.
        if best_ok is not None and best_ok_sim >= MIN_TITLE_SIM_FLOOR:
            return best_ok, best_ok_sim
        return best_any, best_any_sim

    def _label(
        self, p: PolyView, k: KalshiView, sim: float
    ) -> tuple[str, float, list[str], list[str], float | None]:
        """(label, confidence, match_reasons, mismatch_reasons, polymarket_midpoint).

        The Polymarket midpoint is returned only when the outcome side has been
        aligned to the Kalshi YES proposition — otherwise it is absent, and so is
        any observed difference derived from it."""
        match_reasons: list[str] = [f"title_similarity={sim}", f"domain={p.domain}"]
        mismatch: list[str] = list(self._hard_incompatibilities(p, k))
        outcome_ok = outcomes_compatible(p.outcome_type, k.outcome_type)
        if outcome_ok:
            match_reasons.append(f"outcome_type={p.outcome_type}/{k.outcome_type}")
        if p.scope == k.scope and p.scope != SCOPE_EVENT:
            match_reasons.append(f"market_scope={p.scope}")
        if p.sport and p.sport == k.sport:
            match_reasons.append(f"sport={p.sport}")
        shared_entities = p.entities & k.entities
        if shared_entities:
            match_reasons.append(f"shared_entities={','.join(sorted(shared_entities))}")

        res_gap_days = None
        if p.resolution_time and k.resolution_time:
            res_gap_days = round(abs((p.resolution_time - k.resolution_time).total_seconds()) / 86400, 2)
            if res_gap_days <= RESOLUTION_PROXIMATE_DAYS:
                match_reasons.append(f"resolution_proximate_days={res_gap_days}")
            elif res_gap_days > RESOLUTION_MAX_DAYS:
                mismatch.append(f"resolution_gap_days={res_gap_days}")
        else:
            mismatch.append("resolution_time_missing")

        # Side alignment: which Polymarket outcome is the Kalshi market's YES?
        poly_mid, side_reason = poly_yes_midpoint(p, k)
        if side_reason:
            mismatch.append(side_reason)
        else:
            match_reasons.append("outcome_side_aligned")

        # confidence blends title similarity, outcome compatibility, resolution proximity
        conf = 0.6 * sim + (0.25 if outcome_ok else 0.0)
        if res_gap_days is not None and res_gap_days <= RESOLUTION_PROXIMATE_DAYS:
            conf += 0.15
        conf = round(min(1.0, conf), 4)

        hard = [
            r for r in mismatch
            if r.split("=")[0] in (
                "outcome_type_mismatch", REASON_MARKET_TYPE_MISMATCH,
                REASON_SPORT_OR_GAME_MISMATCH, REASON_THRESHOLD_MISMATCH,
                REASON_ENTITY_MISMATCH,
            )
        ]
        if hard:
            return LABEL_INCOMPATIBLE_OUTCOME, conf, match_reasons, mismatch, None
        if res_gap_days is not None and res_gap_days > RESOLUTION_MAX_DAYS:
            return LABEL_INCOMPATIBLE_RESOLUTION, conf, match_reasons, mismatch, None

        # An unaligned side means we do not know WHICH proposition the Polymarket
        # quote prices. That is ambiguity, not contradiction -> unresolved.
        if side_reason:
            return LABEL_UNRESOLVED, conf, match_reasons, mismatch, None

        # An over/under or spread pair whose line cannot be parsed on both sides
        # is unverified, so it may not graduate to `comparable`.
        line_unverified = (
            p.outcome_type == k.outcome_type == _OUTCOME_OVER_UNDER
            and (p.threshold is None or k.threshold is None)
        ) or (
            p.outcome_type == k.outcome_type == _OUTCOME_SPREAD
            and (p.spread_line is None or k.spread_line is None)
        ) or (
            p.outcome_type == k.outcome_type == _OUTCOME_EXACT_SCORE
            and not (set(p.scoreline) & set(k.scoreline))
        )

        if (
            sim >= HIGH_SIM
            and res_gap_days is not None
            and res_gap_days <= RESOLUTION_MAX_DAYS
            and not line_unverified
        ):
            return LABEL_COMPARABLE, conf, match_reasons, mismatch, poly_mid
        if sim >= LOW_SIM:
            return LABEL_LOW_CONFIDENCE, conf, match_reasons, mismatch, poly_mid
        return LABEL_UNRESOLVED, conf, match_reasons, mismatch, poly_mid

    @staticmethod
    def _passes_filters(view, domain: str | None, market_type: str | None) -> bool:
        """A pre-match SAMPLE filter — it narrows which persisted rows are
        considered. It never changes a label or relaxes a precision gate."""
        if domain and view.domain != domain:
            return False
        if market_type and view.outcome_type != market_type:
            return False
        return True

    def match_once(
        self,
        session: Session,
        kalshi_limit: int = 4000,
        polymarket_limit: int = 500,
        persist: bool = True,
        *,
        recent_hours: int | None = None,
        domain: str | None = None,
        market_type: str | None = None,
    ) -> CrossVenueObservationRun:
        started = _now()
        run = CrossVenueObservationRun(status="running", started_at=started, created_at=started)
        if persist:
            session.add(run)
            session.flush()

        try:
            polys_all = self._load_polymarket(session, polymarket_limit)
            kalshi_all, load_mode, stale_skipped = self._load_kalshi(
                session, kalshi_limit, recent_hours=recent_hours
            )

            # Optional sample narrowing (domain / market type). Pre-match filter
            # only: the matcher and every label/gate downstream are unchanged.
            polys = [p for p in polys_all if self._passes_filters(p, domain, market_type)]
            kalshi = [k for k in kalshi_all if self._passes_filters(k, domain, market_type)]

            comparable = unresolved = 0
            candidates: list[CrossVenueMarketCandidate] = []

            for p in polys:
                k, sim = self._best_match(p, kalshi)
                if k is None or sim < MIN_TITLE_SIM_FLOOR:
                    continue  # no plausible comparable — not persisted as noise
                label, conf, mreasons, mismatch, poly_mid = self._label(p, k, sim)

                # Observation metrics (measurement only). A midpoint difference is
                # computed ONLY when the outcome types are compatible AND the
                # Polymarket side has been aligned to the Kalshi YES proposition —
                # otherwise the two numbers price different questions and their
                # difference means nothing.
                mid_diff = None
                if (
                    label in (LABEL_COMPARABLE, LABEL_LOW_CONFIDENCE)
                    and poly_mid is not None and k.midpoint is not None
                ):
                    mid_diff = round(k.midpoint - poly_mid, 4)
                    # A large gap between supposedly identical propositions usually
                    # means the MATCH is wrong (or a quote is stale). Flag it for
                    # human review; never reject on it alone, and never read it as
                    # an opportunity.
                    if abs(mid_diff) > LARGE_OBSERVED_DIFFERENCE and conf < HIGH_SEMANTIC_CONFIDENCE:
                        mismatch.append(f"{REASON_LARGE_DIFFERENCE}={abs(mid_diff)}")
                obs_conf = self._observation_confidence(p, k, poly_mid)

                cand = CrossVenueMarketCandidate(
                    run_id=run.id if persist else None,
                    kalshi_ticker=k.ticker, kalshi_event_ticker=k.event_ticker,
                    polymarket_market_id=p.market_id, polymarket_token_id=p.token_id,
                    polymarket_condition_id=p.condition_id, domain=p.domain,
                    event_title_normalized=" | ".join([normalize_title(k.title), normalize_title(p.question)])[:512],
                    outcome_normalized=p.outcome_type,
                    resolution_time_kalshi=k.resolution_time,
                    resolution_time_polymarket=p.resolution_time,
                    match_confidence=conf, match_label=label,
                    match_reasons=mreasons, mismatch_reasons=mismatch,
                    kalshi_midpoint=k.midpoint, polymarket_midpoint=poly_mid,
                    midpoint_difference=mid_diff,
                    kalshi_spread=k.spread, polymarket_spread=p.spread,
                    kalshi_liquidity_proxy=k.liquidity_proxy,
                    polymarket_liquidity_proxy=p.liquidity_proxy,
                    observed_difference=mid_diff, observation_confidence=obs_conf,
                    raw_context={"title_similarity": sim, "kalshi_snapshot_age_s": k.snapshot_age_seconds},
                    created_at=_now(),
                )
                candidates.append(cand)
                if label == LABEL_COMPARABLE:
                    comparable += 1
                elif label == LABEL_UNRESOLVED:
                    unresolved += 1

            if persist:
                for c in candidates:
                    session.add(c)

            run.status = "ok"
            run.finished_at = _now()
            run.duration_ms = int((run.finished_at - started).total_seconds() * 1000)
            run.kalshi_markets_considered = len(kalshi)
            run.polymarket_markets_considered = len(polys)
            run.candidates_created = len(candidates)
            run.comparable_count = comparable
            run.unresolved_count = unresolved
            run._candidates = candidates  # attached for non-persist callers/tests

            k_domains = _count_by(kalshi, lambda v: v.domain)
            p_domains = _count_by(polys, lambda v: v.domain)
            overlap = sorted(set(k_domains) & set(p_domains))
            run._sample = MatchSampleComposition(
                kalshi_loaded=len(kalshi_all),
                polymarket_loaded=len(polys_all),
                kalshi_considered=len(kalshi),
                polymarket_considered=len(polys),
                kalshi_load_mode=load_mode,
                recent_hours=recent_hours,
                domain_filter=domain,
                market_type_filter=market_type,
                kalshi_stale_skipped=stale_skipped,
                kalshi_without_snapshot=sum(1 for k in kalshi if k.snapshot_age_seconds is None),
                kalshi_by_domain=k_domains,
                polymarket_by_domain=p_domains,
                kalshi_by_market_type=_count_by(kalshi, lambda v: v.outcome_type),
                polymarket_by_market_type=_count_by(polys, lambda v: v.outcome_type),
                overlap_domains=overlap,
                # A representative sample should share domains AND surface at least
                # one candidate; flag when it does not so the operator knows to
                # widen limits or drop the recency window — NOT an opportunity signal.
                low_overlap=(not overlap) or len(candidates) == 0,
            )
            if persist:
                session.commit()
            return run
        except Exception as exc:
            logger.exception("cross-venue match_once failed: %s", exc)
            run.status = "error"
            run.error_type = type(exc).__name__
            run.error_message = str(exc)[:500]
            run.finished_at = _now()
            if persist:
                try:
                    session.commit()
                except Exception:  # pragma: no cover
                    session.rollback()
            raise

    @staticmethod
    def _observation_confidence(p: PolyView, k: KalshiView, poly_mid: float | None) -> float:
        """How much of the observation's supporting data is present. `poly_mid` is
        the SIDE-ALIGNED Polymarket midpoint — an unaligned side counts as missing,
        because a price for an unknown proposition supports nothing."""
        parts = [
            1.0 if poly_mid is not None else 0.0,
            1.0 if k.midpoint is not None else 0.0,
            1.0 if (p.resolution_time and k.resolution_time) else 0.0,
            1.0 if (k.snapshot_age_seconds is not None and k.snapshot_age_seconds < 86400) else 0.0,
        ]
        return round(sum(parts) / len(parts), 4)


# --- report -----------------------------------------------------------------


def _pctile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((pct / 100) * (len(s) - 1)))))
    return round(s[idx], 4)


@dataclass
class CrossVenueReport:
    note: str
    last_run: dict | None
    candidates: int
    by_label: dict = field(default_factory=dict)
    by_domain: dict = field(default_factory=dict)
    comparable: list[dict] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    mismatch_reasons: dict = field(default_factory=dict)
    midpoint_difference: dict = field(default_factory=dict)
    spread_liquidity: dict = field(default_factory=dict)
    freshness: dict = field(default_factory=dict)
    row_counts: dict = field(default_factory=dict)


class CrossVenueReportService:
    """Read-only aggregate over the latest cross-venue observation run."""

    def build(self, session: Session, top: int = 15) -> CrossVenueReport:
        last = session.execute(
            select(CrossVenueObservationRun)
            .where(CrossVenueObservationRun.status == "ok")
            .order_by(CrossVenueObservationRun.id.desc())
        ).scalars().first()

        cands: list[CrossVenueMarketCandidate] = []
        if last is not None:
            cands = session.execute(
                select(CrossVenueMarketCandidate)
                .where(CrossVenueMarketCandidate.run_id == last.id)
            ).scalars().all()

        by_label: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        mismatch: dict[str, int] = {}
        for c in cands:
            by_label[c.match_label] = by_label.get(c.match_label, 0) + 1
            by_domain[c.domain or "other"] = by_domain.get(c.domain or "other", 0) + 1
            for r in (c.mismatch_reasons or []):
                key = str(r).split("=")[0]
                mismatch[key] = mismatch.get(key, 0) + 1

        def row(c: CrossVenueMarketCandidate) -> dict:
            return {
                "kalshi_ticker": c.kalshi_ticker, "polymarket_market_id": c.polymarket_market_id,
                "domain": c.domain, "match_label": c.match_label, "match_confidence": c.match_confidence,
                "kalshi_midpoint": c.kalshi_midpoint, "polymarket_midpoint": c.polymarket_midpoint,
                "observed_difference": c.observed_difference,
                "observation_confidence": c.observation_confidence,
                "title": (c.event_title_normalized or "")[:80],
            }

        comparable = sorted(
            [c for c in cands if c.match_label == LABEL_COMPARABLE],
            key=lambda c: -(c.match_confidence or 0),
        )
        unresolved = [c for c in cands if c.match_label == LABEL_UNRESOLVED]

        diffs = [abs(c.observed_difference) for c in cands if c.observed_difference is not None]
        k_spreads = [c.kalshi_spread for c in cands if c.kalshi_spread is not None]
        p_spreads = [c.polymarket_spread for c in cands if c.polymarket_spread is not None]
        obs_conf = [c.observation_confidence for c in cands if c.observation_confidence is not None]

        return CrossVenueReport(
            note=DISCLAIMER,
            last_run=(
                {
                    "id": last.id, "status": last.status,
                    "kalshi_considered": last.kalshi_markets_considered,
                    "polymarket_considered": last.polymarket_markets_considered,
                    "candidates": last.candidates_created,
                    "comparable": last.comparable_count, "unresolved": last.unresolved_count,
                }
                if last else None
            ),
            candidates=len(cands),
            by_label=by_label,
            by_domain=by_domain,
            comparable=[row(c) for c in comparable[:top]],
            unresolved=[row(c) for c in unresolved[:top]],
            mismatch_reasons=dict(sorted(mismatch.items(), key=lambda kv: -kv[1])),
            midpoint_difference={
                "n": len(diffs),
                "abs_p50": _pctile(diffs, 50), "abs_p90": _pctile(diffs, 90),
                "abs_max": round(max(diffs), 4) if diffs else None,
                "note": "measured probability-point gap |kalshi_mid - polymarket_mid| — not EV/arbitrage",
            },
            spread_liquidity={
                "kalshi_spread_p50": _pctile(k_spreads, 50),
                "polymarket_spread_p50": _pctile(p_spreads, 50),
            },
            freshness={
                "observation_confidence_p50": _pctile(obs_conf, 50),
                "observation_confidence_p90": _pctile(obs_conf, 90),
            },
            row_counts={
                "cross_venue_observation_runs": session.execute(
                    select(func.count()).select_from(CrossVenueObservationRun)
                ).scalar() or 0,
                "cross_venue_market_candidates": session.execute(
                    select(func.count()).select_from(CrossVenueMarketCandidate)
                ).scalar() or 0,
            },
        )
