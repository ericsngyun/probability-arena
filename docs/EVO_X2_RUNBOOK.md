# EVO_X2_RUNBOOK

Host `mikolabs` (Tailscale alias `evo-x2`), user `miko_node_001`, repo at
`~/projects/probability-arena`, `.venv` inside, SQLite at `data/probability_arena.db`.
**Shared production host** — user-level systemd only, never touch other projects'
services or the awaas Docker stack. See `DEPLOYMENT_AUDIT_EVO_X2.md` /
`DEPLOYMENT_REPORT_EVO_X2.md` for history.

## Deployed services (systemd --user; lingering enabled)

| Unit | Cadence | Purpose |
|---|---|---|
| `probability-arena-baseline.timer` | every 4 h | full read-only measurement loop |
| `probability-arena-retention.timer` | daily | prune operational tables |
| `probability-arena-watcher.service` | continuous, 60 s | ticks + informational signals |

> Deployment lag is normal: check `git -C ~/projects/probability-arena log --oneline -1`
> on the host before assuming main is deployed (as of OPS-005 the host is on
> `eeb799d`; OPS-004/MVP-004E/MVP-004F are pending rollout).

## Status

```bash
systemctl --user list-timers | grep probability
systemctl --user status probability-arena-baseline.timer probability-arena-retention.timer probability-arena-watcher.service
cd ~/projects/probability-arena && .venv/bin/python -m app.cli pipeline-status
cd ~/projects/probability-arena && .venv/bin/python -m app.cli db-stats
```

## Logs

```bash
journalctl --user -u probability-arena-baseline.service  -n 100 --no-pager
journalctl --user -u probability-arena-retention.service -n 50  --no-pager
journalctl --user -u probability-arena-watcher.service   -n 100 --no-pager   # PYTHONUNBUFFERED set in unit
```

## Disable / re-enable

```bash
systemctl --user disable --now probability-arena-watcher.service    # stop 60s loop
systemctl --user disable --now probability-arena-baseline.timer     # stop 4h loop
systemctl --user disable --now probability-arena-retention.timer    # stop daily pruning
# re-enable: systemctl --user enable --now <unit>
```

## Deployment update sequence

```bash
cd ~/projects/probability-arena
git status --short                      # must be clean; stop and report if dirty
git pull --ff-only
.venv/bin/pip install -q -r requirements-dev.txt     # if deps changed
.venv/bin/python -m app.cli run-baseline --dry-run   # applies migrations, audit-only
.venv/bin/python -m app.cli db-stats                 # sanity
# restart long-running services after code changes:
systemctl --user restart probability-arena-watcher.service
```

## Feature flag rollout sequence (one flag at a time)

1. Deploy dark (flags unchanged), verify template-mode behavior.
2. Edit `~/projects/probability-arena/.env` — flip exactly one flag (append the key if it predates the `.env`; it was created before newer flags existed).
3. Exercise the smallest possible path (e.g. `process-promoted-signals --limit 3`).
4. Inspect: `research-canary-report`, `signal-report`, journals.
5. Restart the watcher service if the flag affects it (`systemctl --user restart probability-arena-watcher.service` — oneshot timers pick up `.env` next run automatically).
6. Roll back = flip the flag back; no code change needed.

Soccer canary (SOCCER-001) is a two-step rollout: flip `ENABLE_SOCCER_EXTERNAL_RESEARCH=true` first with `SOCCER_RESEARCH_PROVIDER=template` (collector selected, honest fallbacks, zero external calls), inspect `research-canary-report`, then set `SOCCER_RESEARCH_PROVIDER=espn` as its own step.

## DB backup (placeholder — formalize in a later OPS milestone)

SQLite single file; a consistent snapshot while services run:

```bash
cd ~/projects/probability-arena
sqlite3 data/probability_arena.db ".backup data/backup-$(date -u +%Y%m%dT%H%M%SZ).db"
```

TODO: scheduled off-host copies, retention for backups, restore drill.
