# CLAUDE.md

Guidance for Claude Code sessions working in this repo. For what the app
does and how it's architected, read `README.md` first — this file only
covers things that aren't already written down there: standing workflow
conventions, and how to diagnose the class of "Qumulo's UI and snapman
disagree about snapshot space" report that comes up periodically.

## Working conventions

- **Before any backend rebuild/restart**, check for genuinely in-flight work
  first: `docker compose exec -T db psql -U snapman -d snapman -c "SELECT id, status, started_at, source_file_id FROM inspect_jobs WHERE status='running';"`,
  then cross-reference each row's `started_at` against
  `docker inspect --format '{{.State.StartedAt}}' <backend-container>` — a
  "running" row from *before* the container's current start time is a stale
  leftover (jobs don't survive a restart cleanly), not real work. If a row
  looks genuinely active, ask before proceeding rather than assuming it's
  safe to kill.
- **Rebuild**: `docker compose up -d --build --no-deps backend` (or
  `frontend`). Alembic migrations run automatically on container start
  (`Dockerfile`'s `CMD`).
- **Backend tests**: the test directory isn't part of the image. Run them
  with:
  ```
  docker cp backend/test <container>:/app/test
  docker compose exec -T backend python -m unittest discover -s test -v
  docker compose exec -T backend rm -rf /app/test
  ```
- **Frontend typecheck**: `cd frontend && npx tsc --noEmit`.
- **Migrations**: sequential numbered files in `backend/alembic/versions/`
  (`000N_description.py`), each with `revision`/`down_revision` chaining to
  the previous one — follow the existing files' pattern exactly rather than
  letting `alembic revision --autogenerate` name things differently.
- **Admin-configurable settings** (as opposed to deploy-time env vars in
  `backend/app/config.py`) live in the single-row `app_settings` table —
  see `backend/app/routers/admin_settings.py` for the get-or-create +
  `_serialize`/`SettingsUpdate` pattern already established there; add new
  settings the same way rather than inventing a new mechanism.
- Commits end with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.

## Diagnosing "Qumulo says N TB of snapshots, snapman says we can only reclaim M TB"

This comes up because snapman's reclaim figures and Qumulo's own
cluster-capacity dashboard are answering genuinely different questions, even
on a perfectly healthy system — a big gap isn't necessarily a bug. Run
`docker compose exec -T backend python scripts/reclaim_diagnostics.py [display_name]`
(read-only; safe to run anytime, doesn't touch the cluster's snapshots) —
it lists every registered cluster's `display_name`s if you omit the
argument. It checks for the usual suspects directly, worst offenders first:

1. **Undermeasurement (the most common cause by far).** snapman only knows
   the reclaim size of a snapshot pair once something has actually run
   Inspect on it — manually, via a Keep-warm opt-in, or via the goal
   solver. A tree nobody has ever pointed Inspect at contributes exactly 0
   to every total, no matter how much space it actually holds. The script
   reports how many of the cluster's trees are fully measured / partially
   measured / never touched at all, and for each one, `measured/total`
   pairs — a tree showing e.g. `14/1164` almost certainly accounts for a
   large, currently-invisible chunk of the gap. Fix: Inspect (or opt into
   keep-warm, or run the goal solver over) the worst-offending trees the
   script names, then re-run the diagnostic.
2. **Held snapshots capping how deep a tree can be pruned.** A snapshot
   that's locked or replication-owned (`Snapshot.held`/`held_reason` in
   `backend/app/qumulo/api.py`) can never actually be deleted, so no total
   that assumes deleting it is achievable. `held: false` on every snapshot
   the user checked by hand usually only means "not *locked*" — replication
   ownership is a separate, easy-to-miss reason; the script's locked vs.
   replication-owned counts (and each row's `gap reason` column) make this
   explicit either way.
3. **The reclaim curve's cumulative total freezes at the first
   unmeasured/held pair** in a tree's oldest-to-newest sequence
   (`build_points` in `backend/app/qumulo/compute/curve.py`, by design —
   see its docstring). If pair 3 of 30 is blocked, the tree's *displayed*
   cumulative stops there even if pairs 4–30 are fully computed and
   genuinely reclaimable. The script's `raw` column (every live pair
   snapman has actually computed, ignoring the freeze) vs. `displayed`
   column (what the UI shows today) isolates exactly how much this is
   hiding, if anything — a nonzero `hidden` total here is real, already-known
   reclaimable space the UI just isn't surfacing yet for that tree.

In practice, check these roughly in the order above: undermeasurement is
usually the dominant term when the gap is large (tens of TB), since 1–2
unmeasured but large trees can dwarf everything else combined; held
snapshots and the freeze quirk tend to explain smaller, more localized
discrepancies on otherwise-well-measured trees.

The script's own output distinguishes `raw_computed_bytes` (sum of every
individually-computed pair for each tree's *current* live snapshot sequence
— it deliberately ignores stale cache rows left over from snapshots that
have since been deleted, so it isn't inflated by history that's no longer
part of today's curve) from `displayed_bytes` (matches what the Dashboard's
Reclaim~ column / a tree's curve page / the goal solver's ceiling would
show right now). Read both; the delta between them is exactly issue #3
above, and the delta between either of them and Qumulo's own figure is
mostly issue #1.
