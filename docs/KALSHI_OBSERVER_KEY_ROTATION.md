# Rotating the Kalshi observer credential

**Which key.** The **production read-only observer key** — the one behind
`KALSHI_OBSERVER_API_KEY_ID` / `KALSHI_OBSERVER_CREDENTIAL_PATH`. Its scope is
not assumed: the venue attested it at B1 closure, `GET /trade-api/v2/api_keys`
returning scopes exactly `["read"]` with `proven_read_only: true`. It is the
only Kalshi credential this project holds.

**Why rotate.** A truncated SHA-256 fingerprint of this key reached a public
repository. The fingerprint is one-way and the private key never entered the
repo, so this is not a compromise — but rotation makes the published
fingerprint refer to a dead key, which closes the item properly.

---

## Before you start — two hard prerequisites

**1. Do NOT rotate while a capture is running.** The collector reads this
credential at session start. A rotation mid-run, or a failed rotation before a
scheduled window, costs that window — and under a preregistration that can mean
re-running an entire window set. Check first:

```sh
ssh <OBSERVER_HOST> 'pgrep -fa kalshi_activity_profile_day; systemctl --user list-timers --no-pager | head -5'
```

Rotate only in a gap with no timer due before you finish.

**2. The installer OVERWRITES.** It opens the destination `O_TRUNC`, so
installing a new key destroys the old one in place. If the new key turns out to
be wrong, there is no rollback unless you took step 1 below.

---

## The procedure

### Step 1 — back up the current credential (on the host)

```sh
ssh <OBSERVER_HOST> '
  set -e
  cd ~/.config/pa-secrets
  cp -p kalshi-production.pem kalshi-production.pem.bak-$(date -u +%Y%m%dT%H%M%SZ)
  grep -E "^KALSHI_OBSERVER_(API_KEY_ID|CREDENTIAL_PATH)=" ~/projects/probability-arena/.env \
    > ~/.config/pa-secrets/env-observer.bak-$(date -u +%Y%m%dT%H%M%SZ)
  chmod 600 ~/.config/pa-secrets/*.bak-*
  ls -l ~/.config/pa-secrets/ | sed "s/[[:space:]]\+/ /g"
'
```

The backup holds private key material. It is deleted in step 6 and must not
outlive the rotation.

### Step 2 — create the NEW key at Kalshi (browser, you)

Create a second API key with **read-only** scope. **Do not delete the old key
yet** — the old key must keep working until the new one is proven.

You will end up with a **key id** (a UUID) and a **private key PEM** that is
shown once.

### Step 3 — install the new credential (stdin only)

Never paste a secret into a shell argument — it would land in `ps` and in shell
history. The installer accepts stdin only.

```sh
# the private key PEM (multi-line), from the clipboard
pbpaste | ssh <OBSERVER_HOST> 'cd ~/projects/probability-arena && python3 scripts/install_kalshi_credential.py --field key --env production'

# then copy the key id, and:
pbpaste | ssh <OBSERVER_HOST> 'cd ~/projects/probability-arena && python3 scripts/install_kalshi_credential.py --field id --env production'
```

It prints length, mode and a fingerprint — never the value.

### Step 4 — PROVE the new credential before revoking the old

This asks the venue what the installed key can do. It halts unless the key id
appears in this account's key list with scopes exactly `["read"]`.

```sh
ssh <OBSERVER_HOST> 'cd ~/projects/probability-arena && .venv/bin/python scripts/kalshi_prod_capture_p4.py evidence --json' | tail -40
```

Look for the credential gate reporting `passed: true`, `proven_read_only: true`,
and a `key_id_fingerprint` that **differs from the old one**. A same fingerprint
means the new id never took.

**If this fails, STOP and roll back** (step 5). Do not delete the old key.

### Step 5 — rollback, only if step 4 failed

```sh
ssh <OBSERVER_HOST> '
  cd ~/.config/pa-secrets
  cp -p "$(ls -t kalshi-production.pem.bak-* | head -1)" kalshi-production.pem
  chmod 600 kalshi-production.pem
  echo "restored; now restore KALSHI_OBSERVER_API_KEY_ID in .env from env-observer.bak-*"
'
```

Then re-run step 4 to confirm the old credential is working again.

### Step 6 — revoke the old key, then confirm and clean up

Only after step 4 passed:

1. **Delete the OLD key at Kalshi** (browser). The old fingerprint now names a
   dead key, which is the point of the exercise.
2. Re-run **step 4**. It must still pass — this proves the session is running on
   the new key and not on a cached old one.
3. Shred the backups:

```sh
ssh <OBSERVER_HOST> 'cd ~/.config/pa-secrets && shred -u kalshi-production.pem.bak-* env-observer.bak-* 2>/dev/null || rm -f kalshi-production.pem.bak-* env-observer.bak-*; ls -l'
```

### Step 7 — record it

The fingerprint in the contract and P4 evidence is already redacted to
`<KEY_FINGERPRINT_REDACTED>`, so no document needs editing. Note the rotation
date in the milestone log. **Do not write the new fingerprint anywhere in this
repository** — that is what created the incident.

---

## What this does not do

Rotation does not remove the old fingerprint from GitHub history; those blobs
remain reachable by SHA. It makes them worthless, which is the achievable
remediation. It also does not touch the demo credential, which is separate.
