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
from app.services.crypto_tape import DB_LOCKED_MAX_ATTEMPTS, DB_LOCKED_RETRY_SECONDS

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
    assert service["Install"]["WantedBy"] == "default.target"


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
    on_calendar = parse_unit(TIMER_UNIT)["Timer"]["OnCalendar"]
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
        1.0                                               # prelude compute
        + sparse.DEFAULT_MAX_DURATION_SECONDS             # fetch deadline
        + 2.0 * adapter_timeout                           # one in-flight request
        + write_batches * (DB_LOCKED_MAX_ATTEMPTS - 1) * DB_LOCKED_RETRY_SECONDS
        + 1.0                                             # teardown + JSONL append
    )
    return load_independent + blocking_points * w_seconds * load_factor


MEASURED_COMPETING_WRITER_STALL_S = 0.045   # this lane's write phase, measured
WORST_MEASURED_LOAD_FACTOR = 5.80           # dev Mac at load average 5-6
IDLE_MEASURED_LOAD_FACTOR = 1.01            # EVO, idle


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
    for token in ("5.80", "1.01", "42", "146", "45 ms", "SIGTERM"):
        assert token in text, token
    assert "unbounded" in text  # L has no ceiling and the file must not imply one


def test_the_dark_install_contract_is_stated_in_the_service():
    """The unit is meant to be installed while the flag is off. That is only
    safe because `disabled` returns before any read, write, call or telemetry
    append — the file has to say so, and the flag has to still be off."""
    text = SERVICE_UNIT.read_text()
    assert "ENABLE_CRYPTO_SPARSE_OBSERVATION" in text
    assert "status=disabled" in text
    assert Settings(_env_file=None).enable_crypto_sparse_observation is False


# --- nothing here installs, enables, or starts anything ------------------------


def test_the_units_neither_install_nor_enable_anything():
    """`systemctl` appears in both files ONLY inside the commented install
    recipe. A unit that could install or enable something would need an
    `ExecStartPre`/`ExecStartPost`/`ExecStop*` directive, and there is none."""
    for path in (SERVICE_UNIT, TIMER_UNIT):
        parsed = parse_unit(path)
        for section in parsed.sections():
            for key in parsed[section]:
                assert not key.startswith("ExecStartPre"), f"{path.name}: {key}"
                assert not key.startswith("ExecStartPost"), f"{path.name}: {key}"
                assert not key.startswith("ExecStop"), f"{path.name}: {key}"
                assert not key.startswith("ExecReload"), f"{path.name}: {key}"
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
