---
created: 2026-08-12
accessed: 2026-08-12
---

# MEASURED on a live Gitea: the team rename preserves ids, members and repo attachments

The design claim that everything else rested on, checked for the first time against a real server
rather than a fake that models it.

## The claim, and why it was worth stopping to measure

`ensure_team` renames via `PATCH /teams/{id}`. Gitea keys team-user and team-repo on that id, so a
rename preserves grants and there is no window in which a team is empty. **If that were wrong, four
teams lose every grant and every machine silently loses access at the next re-provision** — a
delayed, silent failure.

It had been verified only against an in-memory fake **that models id-keying**, i.e. against the
assumption itself. The auditor who built it said so plainly rather than claiming more.

## The measurement

BEFORE / PROVISION / AFTER on `Tianjie-Zou-Team`, per team, not aggregate:

| | |
|---|---|
| ids | **4–7 preserved**, all four |
| members | unchanged, all four |
| repo attachments | unchanged, all four |
| teams created | **0** — reported as `renamed`, not `updated` |

`Observers` → `Swarm-Observers`, and the other three. `verify` then returned
`configuration problems: none` having actually read teams and protection.

**The claim holds.** Record it as measured, with the date, so the next person does not re-derive it
— and so nobody re-opens it as "unverified" when it now is.

## What the two failed attempts cost, and bought

Both 403s wrote **nothing**: `before.json` and the post-failure re-read were byte-identical. A
provision that cannot get write scopes fails before it mutates, which is the right order.

They also exposed two defects that a success would have hidden forever:

1. **`admin-emit` mints READ-ONLY by design.** It unwedges `list` and can never run `provision`.
   Correct, and previously invisible.
2. **`--admin-token-name` was silently ignored when a stored credential existed.** The operator
   passed the documented unwedge, `token()` returned the stored read-only credential before ever
   reading the flag, and the run failed identically to before the flag existed.

**A remedy that silently does nothing is worse than an absent one.** An absent remedy sends you
looking; a no-op one spends the operator's good idea and returns them to the same error, which reads
as "even the documented fix does not work". Caught because the operator *reported the flag not
working instead of retrying* — and the store was empty by then, so a blind retry would have
SUCCEEDED and buried it.

## The wedge, which was the recovery rather than a gap in it

The admin token name was fixed at `swarmctl-admin@<gethostname()>`. Store and server desync easily
(cleared store, re-imaged box, lost store write, or a 403 whose self-heal erases the local half).
After that every run mints the same taken name, Gitea refuses, and the value is unrecoverable —
Gitea shows it once.

**The only documented exit was `revoke`, which needs the operator's PASSWORD**, because token
management is the one route Gitea refuses to token auth. So a fleet tool built expressly to keep
human credentials out of its own path had a failure mode whose sole remedy was to reach for one.
Same shape as the credential clobber this whole effort began with, arrived at from the other side.

`--admin-token-name` is the passwordless exit. **Explicit, not an automatic suffix**: auto-
disambiguating would silently restore the N-standing-tokens design this file had argued its way out
of, one per desync, with nobody counting.

## Still open

- **The orphan `swarmctl-admin@DUIPEZZTZ` is live and unrevoked.** Provably orphaned — swarmctl is
  its only minter, it stores at mint time, and no store holds it — so nothing can be using it.
  Revoking needs the operator's password. THEIR action.
- **Branch protection on `main` is disabled.** The whole "only the verifier marks a commit green"
  boundary is carried by which process holds which credential, and protection is what makes a green
  mark *matter*. Named here so it is not discovered later as a surprise.
- `swarmctl-admin@ws1-write` (sha256 `63e7d4ea`) is a deliberate standing write credential on the
  host, revocable by name.
