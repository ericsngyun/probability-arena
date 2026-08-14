"""GATE7-SPARSE-UNITS-001 — the sparse observer's systemd service/timer templates.

These units are REPO TEMPLATES. Nothing in this milestone installs, enables or
starts anything, and nothing here touches a network, a provider, or a database.

The tests exist for two joints that no amount of code can hold shut on its own:

  1. THE CADENCE COUPLING. `SPARSE_CADENCE_MINUTES` is the period the code
     assumes and invariant (2) MISSED-PASS TOLERANCE is stated against it, but
     nothing in `app/` can see a systemd `OnCalendar=`. The milestone doc names
     this the weakest joint in the lane. So the unit file is PARSED and its
     `OnCalendar` compared against the constant — not against a comment, not
     against a fenced block in a doc, and not by naive string equality, because
     `hourly` is a systemd ALIAS and a bare string compare would sail straight
     past a cadence change to 30 minutes.
  2. THE TimeoutStartSec DERIVATION. The unit's value is derived from six
     shipped constants. The model is recomputed here from those constants, so
     raising `DEFAULT_MAX_DURATION_SECONDS` (which the deployment plan
     explicitly tells the operator to do) fails the suite instead of silently
     invalidating the number in the unit.
"""

import configparser
import math
import re
from pathlib import Path

import pytest

from app.adapters.dexscreener import DexScreenerAdapter
from app.config import Settings
from app.services import crypto_sparse_observation as sparse
from app.services.crypto_horizon import CryptoHorizonService
from app.services.crypto_tape import DB_LOCKED_MAX_ATTEMPTS, DB_LOCKED_RETRY_SECONDS
from app.telemetry.sink import get_sink, read_events

REPO = Path(__file__).resolve().parents[1]
UNIT_DIR = REPO / "infra/systemd/user"
SERVICE_UNIT = UNIT_DIR / "probability-arena-crypto-sparse-observe.service"
TIMER_UNIT = UNIT_DIR / "probability-arena-crypto-sparse-observe.timer"
RUNBOOK = REPO / "docs/EVO_X2_RUNBOOK.md"


def parse_unit(path: Path) -> configparser.ConfigParser:
    # interpolation=None: systemd specifiers such as %h are literal, not
    # configparser interpolation syntax. strict=False: systemd permits a
    # directive to repeat.
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(path.read_text())
    return parser


def unit_directives(path: Path) -> list[tuple[str, str, str]]:
    """Every `(section, key, value)` a unit file DECLARES, duplicates included.

    `parse_unit` cannot answer the questions the safety pins below ask, and the
    two ways it fails are opposite:

      * IT COLLAPSES DUPLICATES. `strict=False` keeps the LAST value for a
        repeated key, but systemd takes the UNION of repeated `OnCalendar=`
        lines. `OnCalendar=*-*-* *:00,30:00` followed by `OnCalendar=hourly`
        reads to configparser as `hourly` and fires to systemd every 30
        minutes, which is a doubled provider bill invisible to the cadence pin.
      * IT CANNOT SEE ABSENCE FROM PROSE. These files are 80% comment, so a
        `key in section` test is only as good as the parse — and a naive
        `"Environment=" in text` grep is worse in both directions at once: it
        trips on a comment that merely NAMES the directive, and it is SATISFIED
        by a comment that merely names it.

    So this reads directives and nothing else: `[Section]` headers, `key=value`
    lines, and skipping blanks and `#`/`;` comments the way systemd does. A
    comment can neither trip a ban nor satisfy a requirement here.
    """
    directives: list[tuple[str, str, str]] = []
    section = ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        directives.append((section, key.strip(), value.strip()))
    return directives


def directive_values(path: Path, key: str) -> list[str]:
    """Every value declared for `key`, in file order. Empty if never declared."""
    return [v for _s, k, v in unit_directives(path) if k == key]


def oncalendar_interval_minutes(value: str) -> float:
    """The firing INTERVAL, in minutes, that a systemd `OnCalendar=` implies.

    This is the whole point of the pin. `hourly` is an alias systemd expands to
    `*-*-* *:00:00`; comparing the literal string `"hourly"` to anything derived
    from `SPARSE_CADENCE_MINUTES` would compare a name to a number and pass for
    the wrong reason. Only the forms this lane can legitimately produce are
    accepted — anything else raises, because an unrecognised calendar spec means
    the coupling is no longer being checked, and silently returning `None` there
    is exactly how a pin stops pinning.
    """
    value = value.strip()
    aliases = {"minutely": 1.0, "hourly": 60.0, "daily": 1440.0}
    if value in aliases:
        return aliases[value]
    # `*-*-* 00/H:00:00` — every H hours from midnight
    m = re.fullmatch(r"\*-\*-\* 00/(\d+):00:00", value)
    if m:
        return float(m.group(1)) * 60.0
    # `*-*-* *:00/M:00` — every M minutes within every hour
    m = re.fullmatch(r"\*-\*-\* \*:00/(\d+):00", value)
    if m:
        return float(m.group(1))
    raise AssertionError(
        f"OnCalendar={value!r} is not a form this test can convert to an "
        "interval, so the cadence coupling is NOT being checked. Extend this "
        "function deliberately, or put the timer back on a form "
        "`timer_oncalendar()` can produce."
    )


def timespan_seconds(value: str) -> float:
    """A systemd timespan (`5min`, `300`, `1s`, `1us`) in seconds."""
    value = value.strip()
    units = {
        "us": 1e-6, "usec": 1e-6, "ms": 1e-3, "msec": 1e-3,
        "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0,
        "m": 60.0, "min": 60.0, "minute": 60.0, "minutes": 60.0,
        "h": 3600.0, "hr": 3600.0, "hour": 3600.0, "hours": 3600.0,
    }
    total = 0.0
    matched = False
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]*)", value):
        matched = True
        # A bare number is seconds in systemd. An UNRECOGNISED suffix must fail
        # loudly rather than arithmetic-error: this helper is what the
        # TimeoutStartSec pin compares against, and a silently mis-scaled
        # timespan would make that pin pass for the wrong reason.
        assert unit == "" or unit in units, (
            f"unrecognised systemd time unit {unit!r} in {value!r}")
        total += float(number) * (1.0 if unit == "" else units[unit])
    assert matched, f"unparsable systemd timespan {value!r}"
    return total


# --- the units exist, and are the shape this host accepts ----------------------


def test_both_unit_files_exist_and_parse():
    assert SERVICE_UNIT.exists() and TIMER_UNIT.exists()
    parse_unit(SERVICE_UNIT)
    parse_unit(TIMER_UNIT)


def test_service_is_a_user_level_oneshot_with_no_loop_of_its_own():
    service = parse_unit(SERVICE_UNIT)
    assert service["Service"]["Type"] == "oneshot"
    text = SERVICE_UNIT.read_text()
    assert "Restart=" not in text
    assert "User=" not in text and "Group=" not in text  # user manager only
    assert "sudo" not in text
    # recurrence belongs to the timer, never a loop inside the service
    for banned in ("while true", "--loop", "\nsleep ", "crontab", "daemon"):
        assert banned not in text, banned


def test_the_service_declares_no_install_section():
    """A timer-driven `Type=oneshot` must not be independently enable-able.

    This DEVIATES from the reconciler's service unit, which carries
    `[Install] WantedBy=default.target`, and the deviation is the point: the
    timer's `Unit=` is what pulls this service in, so an `[Install]` here buys
    nothing and leaves a latent hazard. A stray
    `systemctl --user enable …-crypto-sparse-observe.service` — easy to type
    when you meant `.timer` — would install a `default.target` want, and this
    PROVIDER-SPENDING lane would then run a full pass at every login, off the
    schedule, with invariant (2)'s lattice argument silent about the extra
    firings. Precedent does not justify propagating an unsafe default into a
    lane the precedent did not consider: the reconciler is provider-free.

    MUTATION: restore `[Install] WantedBy=default.target` and this fails.
    """
    sections = {section for section, _k, _v in unit_directives(SERVICE_UNIT)}
    assert "Install" not in sections, (
        "the sparse .service has grown an [Install] section; a stray "
        "`systemctl --user enable` on the SERVICE would then run a "
        "provider-spending pass at every login, off the timer's schedule"
    )
    assert directive_values(SERVICE_UNIT, "WantedBy") == []
    assert directive_values(SERVICE_UNIT, "RequiredBy") == []
    assert directive_values(SERVICE_UNIT, "Also") == []
    # The TIMER keeps its [Install] — that is the unit an operator enables, and
    # `timers.target` is inert until they do.
    assert directive_values(TIMER_UNIT, "WantedBy") == ["timers.target"]


def test_the_service_takes_its_environment_only_from_the_env_file():
    """THE GATE IS CROSSED IN `.env`, NEVER IN THE UNIT.

    The whole safety story of this milestone is "install the units dark, flip
    the flag later, deliberately". An `Environment=` directive in the service
    would end that story at install time: systemd applies `Environment=` AFTER
    `EnvironmentFile=` when it appears later in the unit, so a single line

        Environment=ENABLE_CRYPTO_SPARSE_OBSERVATION=true

    arms the lane the moment the file is copied, overriding the `.env` the
    disable procedure in the runbook edits — and the operator's `grep` on
    `.env` would still show `false`. Nothing else in this suite bans it: the
    unit-name list, the `NOT auto-installed` string, the `systemctl` check and
    the dark-contract test (which asserts only that the STRING
    `ENABLE_CRYPTO_SPARSE_OBSERVATION` occurs, and an arming directive contains
    it) are all satisfied by a service carrying that line.

    So: no `Environment=` at all, and `EnvironmentFile=` exactly once as the
    sole env source. Asserted against PARSED DIRECTIVES — a comment naming
    `Environment=` (there are several) neither trips this nor satisfies it.

    MUTATION: add `Environment=ENABLE_CRYPTO_SPARSE_OBSERVATION=true` to the
    service and this fails; delete the `EnvironmentFile=` line and this fails.
    """
    for path in (SERVICE_UNIT, TIMER_UNIT):
        assert directive_values(path, "Environment") == [], (
            f"{path.name}: an `Environment=` directive can arm "
            "ENABLE_CRYPTO_SPARSE_OBSERVATION at install time and takes "
            "precedence over the .env file the runbook's disable procedure "
            "edits. The gate is crossed in .env, never in the unit."
        )
        # The rest of systemd's env-injection surface, for completeness: each of
        # these can put a value in the pass's environment without appearing in
        # `.env`, and none of them has a use in this lane.
        for banned in ("PassEnvironment", "UnsetEnvironment",
                       "EnvironmentFileOverride"):
            assert directive_values(path, banned) == [], f"{path.name}: {banned}"

    env_files = directive_values(SERVICE_UNIT, "EnvironmentFile")
    assert len(env_files) == 1, (
        f"expected exactly one EnvironmentFile= directive, got {env_files}; "
        "a second one is a second, unreviewed place the flag can be set"
    )
    assert env_files[0].endswith("projects/probability-arena/.env")
    assert directive_values(TIMER_UNIT, "EnvironmentFile") == []


def test_service_uses_the_fixed_venv_python_and_project_paths():
    service = parse_unit(SERVICE_UNIT)
    exec_start = service["Service"]["ExecStart"]
    assert ".venv/bin/python" in exec_start
    assert "-m app.cli crypto-sparse-observe" in exec_start
    assert service["Service"]["WorkingDirectory"].endswith(
        "projects/probability-arena")
    assert service["Service"]["EnvironmentFile"].endswith(".env")
    assert service["Service"]["NoNewPrivileges"] == "true"


def test_execstart_carries_no_gate_bypass_argument():
    """A scheduled run must be a GENUINE scheduled run.

    `run_source` is derived from systemd's `INVOCATION_ID`, so every run of this
    unit files as `run_source="scheduled"`. `--force` sets `gate_bypassed=True`,
    and `scheduled` + `gate_bypassed=True` is defined in
    `app/telemetry/writer_pass.py` as a real anomaly — putting `--force` in the
    unit would file every timer run as an attended bypass and destroy the signal
    that catches a genuine one. `--dry-run` would make the schedule produce no
    observations at all while looking green.
    """
    exec_start = parse_unit(SERVICE_UNIT)["Service"]["ExecStart"]
    assert "--force" not in exec_start
    assert "--dry-run" not in exec_start
    # ...and no override of the limits the TimeoutStartSec derivation assumes.
    for banned in ("--enrol-limit", "--observe-limit", "--write-batch-size",
                   "--max-duration-seconds"):
        assert banned not in exec_start, banned


# --- THE CADENCE PIN -----------------------------------------------------------


def test_the_timer_oncalendar_is_the_one_the_cadence_constant_derives():
    """MUTATION: set `SPARSE_CADENCE_MINUTES` to anything but 60 and this fails.

    Two independent assertions on the SAME parsed directive, because either one
    alone has a hole:
      * equality with `timer_oncalendar()` catches a constant that moved without
        the unit, but would also pass if both moved to a form that means
        something else;
      * the interval conversion catches a hand-edited calendar spec that agrees
        with no constant at all, and is what makes the `hourly` alias explicit
        rather than a lucky string match.
    """
    # DUPLICATES FIRST, because the two parsers disagree about them and systemd
    # sides with the one this pin was NOT using. `parse_unit` is
    # `strict=False`, so it keeps the LAST value for a repeated key; systemd
    # takes the UNION of every `OnCalendar=`. A file carrying
    #   OnCalendar=*-*-* *:00,30:00
    #   OnCalendar=hourly
    # passes every assertion below while firing every 30 minutes — doubling the
    # provider bill and halving the lattice gap the jitter test reasons about,
    # invisibly.
    declared = directive_values(TIMER_UNIT, "OnCalendar")
    assert len(declared) == 1, (
        f"expected exactly one OnCalendar= directive, got {declared}; systemd "
        "takes the UNION of repeated OnCalendar lines, so the cadence pin "
        "below would be checking only the last one"
    )
    on_calendar = parse_unit(TIMER_UNIT)["Timer"]["OnCalendar"]
    assert on_calendar == declared[0]
    assert on_calendar == sparse.timer_oncalendar(), (
        "the installed OnCalendar is no longer what SPARSE_CADENCE_MINUTES "
        f"derives ({sparse.timer_oncalendar()!r}); the systemd schedule and "
        "invariant (2) MISSED-PASS TOLERANCE have de-synchronised, and nothing "
        "in app/ can see this file"
    )
    assert oncalendar_interval_minutes(on_calendar) == sparse.SPARSE_CADENCE_MINUTES


def test_the_timer_spends_no_jitter_it_does_not_have():
    """Invariant (2) needs >= 2 passes inside every closed band of 2*BAND.

    At the shipped pair (BAND 60, CADENCE 60) that holds with EXACTLY ZERO
    slack: a lattice of gap g puts 2 points in a 120-minute closed interval only
    while g <= 60 min. So `RandomizedDelaySec` must be 0 and `AccuracySec` must
    be shrunk from systemd's 1-minute default, which on its own can produce a
    61-minute gap.

    MUTATION: restore `RandomizedDelaySec=300` (the reconciler's value) and this
    fails; drop `AccuracySec` back to the systemd default and this fails.
    """
    timer = parse_unit(TIMER_UNIT)["Timer"]
    randomized_s = timespan_seconds(timer.get("RandomizedDelaySec", "0"))
    # systemd's default when the directive is absent is 1min.
    accuracy_s = timespan_seconds(timer.get("AccuracySec", "1min"))
    jitter_s = randomized_s + accuracy_s

    band_s = sparse.SPARSE_BAND_MINUTES * 60.0
    cadence_s = sparse.SPARSE_CADENCE_MINUTES * 60.0
    # slack = how much a gap may exceed the cadence before a closed band of
    # length 2*BAND stops containing two lattice points. 0 at the shipped pair.
    slack_s = 2.0 * band_s - 2.0 * cadence_s
    assert jitter_s <= max(1.0, slack_s), (
        f"scheduler jitter of {jitter_s}s against a containment slack of "
        f"{slack_s}s: one stretched gap leaves a band with a single scheduled "
        "pass, which is the state invariant (2) exists to prevent"
    )
    assert randomized_s == 0.0


def test_the_timer_owns_the_recurrence_and_defers_a_missed_pass():
    timer = parse_unit(TIMER_UNIT)["Timer"]
    assert timer["Unit"] == "probability-arena-crypto-sparse-observe.service"
    assert timer["Persistent"] == "false"
    assert parse_unit(TIMER_UNIT)["Install"]["WantedBy"] == "timers.target"
    # The recurrence is a calendar, never an `OnUnitActiveSec` loop off a boot
    # anchor (which is what puts a unit on the MarketOps grid at every boot).
    # Asserted against the PARSED directives, not the file text: the comments
    # name those directives while explaining why this timer does not use them.
    assert "OnUnitActiveSec" not in timer
    assert "OnBootSec" not in timer
    assert "OnStartupSec" not in timer


# --- THE TimeoutStartSec PIN ---------------------------------------------------


def sparse_pass_model(w_seconds: float, load_factor: float) -> float:
    """The unit's own wall-time model, recomputed from the shipped constants.

    total(w, L) = N + B * w * L, where w is the per-lock-acquisition wait and L
    is the host's SQLite busy-timeout overshoot factor. The arithmetic and every
    justification live in the .service file; this is the executable half.
    """
    enrol_commits = math.ceil(
        sparse.DEFAULT_ENROL_LIMIT / sparse.DEFAULT_WRITE_BATCH_SIZE)
    write_batches = math.ceil(
        sparse.DEFAULT_OBSERVE_LIMIT / sparse.DEFAULT_WRITE_BATCH_SIZE)
    prelude_reads = 8          # MarketOps health, cohort, index probe, candidates, plan
    write_preloads = 2         # `members`, `retry_rows`
    blocking_points = (
        prelude_reads
        + enrol_commits                                   # no ladder, by design
        + write_preloads
        + write_batches * DB_LOCKED_MAX_ATTEMPTS * 2      # prepare() + commit()
    )
    adapter_timeout = DexScreenerAdapter(settings=Settings(_env_file=None)).timeout
    load_independent = (
        INTERPRETER_START_ALLOWANCE_S                     # `TimeoutStartSec` on a
        # `Type=oneshot` bounds the WHOLE process, imports included.
        + 1.0                                             # prelude compute
        + sparse.DEFAULT_MAX_DURATION_SECONDS             # fetch deadline
        # One in-flight request. `Timeout(15.0)` sets connect, write, read AND
        # pool to 15 s EACH, so the arithmetically complete charge for a
        # request that stalls in every phase it performs is 3 x 15 s, not the
        # 2 x this model used to carry. Pool is not charged: the adapter issues
        # requests sequentially against an uncontended client, so acquisition
        # does not queue. A 4th 15 s would not move the verdict either.
        + 3.0 * adapter_timeout
        + write_batches * (DB_LOCKED_MAX_ATTEMPTS - 1) * DB_LOCKED_RETRY_SECONDS
        + 1.0                                             # teardown + JSONL append
    )
    return load_independent + blocking_points * w_seconds * load_factor


# The smallest of the source measurement's three phase figures — the write
# phase (fetch was 28 ms; the PRELUDE was 2,023 ms). See the citation note in
# the .service: this is w for the B term's largest contributor, and the prelude
# acquisitions are carried at the same w as an admitted simplification.
MEASURED_COMPETING_WRITER_STALL_S = 0.045   # this lane's write phase, measured
WORST_MEASURED_LOAD_FACTOR = 5.80           # dev Mac at load average 5-6
IDLE_MEASURED_LOAD_FACTOR = 1.01            # EVO, idle
# Interpreter start plus `import app.cli` (SQLAlchemy, pydantic, httpx), which
# `TimeoutStartSec` covers on a `Type=oneshot` and the model used to omit.
# Measured on the dev Mac at load average 3-4: 0.41 / 0.44 / 0.67 s wall.
INTERPRETER_START_ALLOWANCE_S = 3.0         # ~4.5x the worst measurement


def test_timeout_start_sec_covers_the_measured_regime_it_was_derived_from():
    """MUTATION: raise `DEFAULT_MAX_DURATION_SECONDS` (which the deployment plan
    tells the operator to re-set from a measured attended pass) or
    `DEFAULT_OBSERVE_LIMIT`, and this fails — which is the moment the number in
    the unit stops being derived and starts being a leftover.
    """
    service = parse_unit(SERVICE_UNIT)
    timeout_s = timespan_seconds(service["Service"]["TimeoutStartSec"])
    worst_measured = sparse_pass_model(
        MEASURED_COMPETING_WRITER_STALL_S, WORST_MEASURED_LOAD_FACTOR)
    assert timeout_s >= worst_measured, (
        f"TimeoutStartSec={timeout_s}s no longer covers the measured-regime "
        f"model ({worst_measured:.1f}s). Re-derive it in the unit file."
    )
    # ...and it is not so generous that it stops being a bound. The whole
    # measured-regime margin exists to absorb `w`; 4x would be a different,
    # undocumented decision.
    assert timeout_s <= 4.0 * worst_measured


def test_timeout_start_sec_kills_a_hung_pass_before_the_next_tick():
    """A pass that outlives its own cadence would meet the next one. The overlap
    flock makes that a typed `skipped_overlap` rather than a race, but a lane
    that can only ever report `skipped_overlap` has stopped observing, so the
    timeout must land first."""
    timeout_s = timespan_seconds(
        parse_unit(SERVICE_UNIT)["Service"]["TimeoutStartSec"])
    assert timeout_s < sparse.SPARSE_CADENCE_MINUTES * 60.0


def test_the_service_states_the_derivation_and_the_exceedance_conditions():
    """The reconciler's precedent: the arithmetic, the factor at which the
    chosen value is exceeded, and the non-corrupting outcome are all IN THE
    FILE, so the next reader does not re-derive them."""
    text = SERVICE_UNIT.read_text()
    for token in ("5.80", "1.01", "42", "164", "45 ms", "SIGTERM"):
        assert token in text, token
    assert "unbounded" in text  # L has no ceiling and the file must not imply one
    # The two measured-regime figures the file TABULATES are recomputed, so the
    # table cannot go stale while the coverage assertion above still passes.
    for load_factor in (IDLE_MEASURED_LOAD_FACTOR, WORST_MEASURED_LOAD_FACTOR):
        figure = sparse_pass_model(MEASURED_COMPETING_WRITER_STALL_S, load_factor)
        assert f"{figure:.1f}" in text, (
            f"the unit's tabulated figure for L = {load_factor} is stale; the "
            f"model now gives {figure:.1f} s"
        )


def test_the_dark_install_contract_is_stated_in_the_service():
    """The unit is meant to be installed while the flag is off. That is only
    safe because `disabled` returns before any read, write, call or telemetry
    append — the file has to say so, and the flag has to still be off.

    THE PROSE HALF ONLY. The claim itself is executed by
    `test_a_dark_pass_touches_no_row_no_provider_and_no_sink` below.
    """
    text = SERVICE_UNIT.read_text()
    assert "ENABLE_CRYPTO_SPARSE_OBSERVATION" in text
    assert "status=disabled" in text
    assert Settings(_env_file=None).enable_crypto_sparse_observation is False


class _ExplodingSession:
    """A `Session` that fails any use at all. The dark pass must never reach it.

    Every entry point a SQLAlchemy `Session` offers this lane raises, so the
    test cannot pass by a stub quietly returning an empty result: the pass
    either returns before touching the session or the test errors.
    """

    def __getattr__(self, name):
        def _boom(*_a, **_k):
            raise AssertionError(
                f"a dark pass touched the database: Session.{name}() was "
                "called with ENABLE_CRYPTO_SPARSE_OBSERVATION off"
            )
        return _boom


class _ExplodingAdapter:
    """The provider surface `CryptoHorizonService` calls. A dark pass spends
    no requests, so any call here is a real DexScreener request this lane
    would have paid for at install time."""

    source_name = "dexscreener"

    def __init__(self):
        self.calls = 0

    async def fetch_pairs_for_token(self, token_address):
        self.calls += 1
        raise AssertionError(
            f"a dark pass called the provider for {token_address!r}")


async def test_a_dark_pass_touches_no_row_no_provider_and_no_sink(tmp_path):
    """THE DARK-INSTALL CONTRACT, EXECUTED.

    The .service file's central safety claim — the one that makes step 5 of the
    runbook's enable procedure ("install the units dark") legitimate — is that
    with the flag off `run_scheduled_sparse_observation` returns from its FIRST
    branch, "before the overlap lock, before the MarketOps health read, before
    the cohort lookup — so no row is read, no row is written, no provider is
    called and no telemetry record is appended".

    Until now that claim existed as PROSE plus a `Settings(...) is False`
    check, which proves only the default, not the behaviour. Given the entire
    deployment intent is "install dark and flip later", the claim is worth an
    executable pin: an operator who copies these units onto EVO-X2 is trusting
    it, and a future edit that moved any of that work above the gate would
    spend provider requests, and take a lock, on a host that installed the
    timer precisely because it believed it would not.

    Each half is proved by a surface that FAILS rather than one that records:
    the session raises on any attribute use, the adapter raises on any fetch.
    Telemetry is the exception — it is asserted by counting, because
    `_emit_pass_telemetry` swallows everything by design and a raising sink
    would prove nothing.

    MUTATION: move the gate below the MarketOps health read, or route the
    `disabled` return through `_finish`, and this fails.
    """
    settings_off = Settings(_env_file=None, database_url="sqlite://")
    assert settings_off.enable_crypto_sparse_observation is False, (
        "this test is meaningless if the flag defaults on")

    adapter = _ExplodingAdapter()
    service = CryptoHorizonService(adapter=adapter, settings=settings_off)

    sink = get_sink()
    before = sink.emitted

    result = await sparse.run_scheduled_sparse_observation(
        _ExplodingSession(), settings=settings_off, service=service,
        sleeper=lambda _s: None,
    )

    # The typed no-op the unit's comment quotes, not merely "did not crash".
    assert result["status"] == sparse.STATUS_DISABLED
    assert result["flag"] == sparse.FLAG
    assert result["gate_bypassed"] is None
    assert result.get("external_calls", 0) == 0

    assert adapter.calls == 0, "a dark pass spent provider requests"

    # ZERO TELEMETRY APPENDS. The `disabled` return deliberately does not pass
    # through `_finish`, so an hourly dark timer files nothing — otherwise a
    # dark install would write 24 records a day describing nothing, into the
    # shared 001A sink whose per-writer counts a calibration corpus reads.
    assert sink.emitted == before, "a dark pass appended a telemetry record"
    assert sink.dropped == 0 and sink.rejected == 0
    if Path(sink.path).exists():
        events, malformed = read_events(sink.path)
        assert (events, malformed) == ([], 0), (
            f"a dark pass wrote to the telemetry sink: {events}")


# --- nothing here installs, enables, or starts anything ------------------------


def test_the_units_neither_install_nor_enable_anything():
    """`systemctl` appears in both files ONLY inside the commented install
    recipe. The ONE `ExecStart=` is the whole execution surface: any other
    `Exec*` directive is a second program this unit runs, and a second program
    is how a template installs itself.

    `ExecCondition=` is in the ban list and is not decoration. systemd runs it
    BEFORE `ExecStart=`, so `ExecCondition=/path/install.sh` is exactly the
    hazard `ExecStartPre=/path/install.sh` is banned for, reached one step
    earlier — and it was the one execution vector this test used to leave open.

    Asserted over DECLARED DIRECTIVES rather than the parsed mapping, so a
    repeated `Exec*` cannot hide behind `strict=False` deduplication.
    """
    banned_prefixes = (
        "ExecStartPre", "ExecStartPost", "ExecStop", "ExecReload",
        "ExecCondition",
    )
    for path in (SERVICE_UNIT, TIMER_UNIT):
        for _section, key, value in unit_directives(path):
            for prefix in banned_prefixes:
                assert not key.startswith(prefix), (
                    f"{path.name}: {key}={value} — systemd runs this as a "
                    "second program, which is how an uninstalled template "
                    "installs itself"
                )
        # ...and the one permitted Exec* directive is permitted exactly once.
        assert len(directive_values(path, "ExecStart")) == (
            1 if path is SERVICE_UNIT else 0), path.name
        for line in path.read_text().splitlines():
            if "systemctl" in line:
                assert line.lstrip().startswith("#"), (
                    f"{path.name}: systemctl outside a comment: {line!r}"
                )
        assert "NOT auto-installed" in path.read_text()


def test_the_units_carry_no_forbidden_vocabulary():
    for path in (SERVICE_UNIT, TIMER_UNIT):
        body = path.read_text().lower()
        # Same set as the tick-aggregation units' governed check. `wallets` and
        # `keys` are deliberately NOT banned: both files state the boundary as a
        # negation ("no wallets, no keys, no swaps"), exactly as the reconciler's
        # units do, and banning the word would delete the boundary statement.
        for bad in ("arbitrage", "opportunity", "paper trad", "position siz"):
            assert bad not in body, f"{path.name}: {bad}"
    service = SERVICE_UNIT.read_text().lower()
    assert "measurement only, never advice" in service


def test_the_reconciler_units_are_untouched_by_this_milestone():
    """This milestone authors ONE new pair. The reconciler's reviewed units are
    out of scope and must not have acquired a sparse reference."""
    for name in ("probability-arena-crypto-reconcile.service",
                 "probability-arena-crypto-reconcile.timer"):
        assert "sparse" not in (UNIT_DIR / name).read_text().lower()


# --- the runbook ---------------------------------------------------------------


def test_the_runbook_puts_the_disable_procedure_before_the_enable_procedure():
    runbook = RUNBOOK.read_text()
    disable = runbook.find("#### Disable the sparse observer timer")
    enable = runbook.find("#### Enable the sparse observer timer")
    assert disable != -1 and enable != -1
    assert disable < enable, "an operator in trouble must find DISABLE first"
    assert "ENABLE_CRYPTO_SPARSE_OBSERVATION=false" in runbook


def test_the_runbook_documents_the_post_install_timer_inventory():
    """A later audit needs a list to diff against, not a memory."""
    runbook = RUNBOOK.read_text()
    marker = "#### Expected timer inventory after install"
    assert marker in runbook
    section = runbook[runbook.find(marker):][:4000]
    assert "probability-arena-crypto-sparse-observe.timer" in section
    assert "systemctl --user list-timers" in section


@pytest.mark.parametrize("value,expected", [
    ("hourly", 60.0),
    ("daily", 1440.0),
    ("*-*-* 00/6:00:00", 360.0),
    ("*-*-* *:00/30:00", 30.0),
])
def test_the_oncalendar_converter_reads_the_forms_this_lane_can_emit(value, expected):
    """The pin is only as good as this conversion, so it is tested directly —
    including the alias, which is the form the shipped cadence produces and the
    one a naive string compare would get wrong."""
    assert oncalendar_interval_minutes(value) == expected


def test_the_oncalendar_converter_refuses_a_form_it_cannot_check():
    with pytest.raises(AssertionError):
        oncalendar_interval_minutes("*-*-* 03,09,15,21:07:00")
