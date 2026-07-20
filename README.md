# snapman-v2

A multi-user, web-based tool for analyzing and reclaiming space consumed by Qumulo
snapshots, for clusters running **Qumulo Core 7.9.0+** (the "post-9.7" snapshot
model).

## Why this exists

Qumulo Core reports the *total* capacity consumed by snapshots across a cluster, but
it does not report a per-snapshot number — there's no API call that answers "how much
space would I get back if I deleted snapshot X?" That number has to be *computed*, by
diffing snapshots against each other and figuring out which physical data blocks are
uniquely tied to which snapshot versus shared with others.

This tool does that computation against the cluster's snapshot-diff APIs, caches the
results, and presents them through a web UI so an operations team can make informed
snapshot-cleanup decisions without installing CLI tooling or writing scripts. The
underlying methodology is a direct port of a CLI tool (`qsnap`), rebuilt as a
containerized, multi-user, multi-cluster web app.

## Core concepts

Three different questions can be asked about snapshot space, and this tool answers
all three — they are **not** interchangeable, and the same snapshot can report a
different number depending on which question you're asking.

### 1. "If I delete everything older than a date, what do I get back?"

This is a **prefix** deletion — the classic retention-policy question ("keep the last
30 days"). Answered by the **reclaim curve**, computed by walking every consecutive
pair of snapshots in a tree's history and diffing each pair. Diffing pair `(older,
newer)` tells you the bytes `older` holds that `newer` doesn't — i.e. what's freed if
you delete `older` *and everything before it*. Summing consecutive pairs from the
oldest snapshot forward gives a cumulative "delete everything before this date"
curve. Triggered by the **Inspect** / **Re-inspect** button on a tree's detail page.

### 2. "If I delete just this one snapshot, what do I get back?"

Not a prefix — deleting one snapshot alone, keeping *both* its older and newer
neighbors. A block of data is only freed by this if it's absent from **both**
neighbors (created after the older one, overwritten before the newer one) — data
that's merely shared with one neighbor doesn't count, because that neighbor still
holds a reference to it. This needs a three-way diff (against both neighbors, not
just one), computed by the **Size snapshots** button, shown as "Individual size" in
the Snapshot sizes table. It is usually smaller than what the reclaim curve implies
for the same snapshot — often zero, if the snapshot's data is entirely shared with a
neighbor.

The oldest snapshot in a tree is a special case: since it has no older neighbor, its
individual size is identical to the reclaim-curve pairwise number, and needs no extra
computation. The newest snapshot can never be sized this way — there's no live-
filesystem diff API to compare it against, so it's always reported as "not sizable".

### 3. "If I delete this specific *set* of snapshots together, what do I get back?"

The general case, covering an arbitrary hand-picked selection — not necessarily a
prefix, not necessarily just one snapshot. Selected snapshots are grouped into
contiguous runs (bounded by whichever snapshots remain kept), and each run's true
total is:

```
sum of adjacent pairwise diffs spanning the run  −  one direct diff between the run's two kept boundaries
```

The subtraction matters: naively summing each selected snapshot's *individual* size
undercounts whenever two selected snapshots are adjacent, because data shared *only
between the selected snapshots* gets missed by each one's solo number. This is
triggered by checking multiple rows in the Snapshot sizes table and clicking
**"Estimate combined savings"**. For a single selected snapshot, this formula reduces
algebraically to exactly the individual-size number from question 2 — it's a
generalization, not a competing calculation.

### Held snapshots

A snapshot that's **locked** or **replication-owned** can't be deleted through this
tool regardless of what any of the above numbers say. Pairs/sets involving a held
snapshot are skipped by default (there's an "Include locked/replication-held
snapshots" checkbox to measure them anyway, e.g. for audit purposes) — mainly because
a held snapshot is often a long-lived anchor (e.g. a replication base) with a huge
time gap to its next surviving neighbor, and diffing across that gap can be extremely
expensive for a number that isn't actionable anyway.

### Accuracy notes

- Numbers are **logical data bytes** as the diff APIs report them — they don't
  include metadata or data-protection (erasure-coding) overhead, so actual freed
  space on disk will differ somewhat.
- If a parent or child path (or the cluster root) was *also* snapshotted during a
  tree's history, its snapshots may share data with the tree being analyzed;
  affected numbers are marked as upper bounds (`≤`) on the group overview.
- A file renamed between two snapshots can be over-counted.
- The snapshot listing is cached for 5 minutes per cluster (avoids hammering the
  cluster's API on every page load) — use the **Refresh** button on the dashboard to
  bypass that after adding/deleting snapshots directly on the cluster. Deleting
  snapshots *through this app* refreshes the cache automatically.

## Screens

### Login

Local accounts only (no LDAP/SSO). The first time the app starts with an empty user
table, it creates a `admin` user from the `ADMIN_PASSWORD` environment variable (or
generates and logs a random one if that's not set — check `docker compose logs
backend` in that case).

### Dashboard (cluster list + group overview)

- Left sidebar: your registered clusters (all of them, if you're an admin; just your
  own otherwise). Add a cluster with either a bearer token or a Qumulo
  username/password (exchanged server-side for a session token — credentials
  themselves are never stored, only the resulting token, encrypted at rest). Hover a
  cluster for **Edit** (including rotating its credentials) and **Delete**.
- Main panel, once a cluster is selected: every snapshotted source path ("tree") on
  that cluster, with:
  - **Snaps** — how many snapshots exist for that tree.
  - **Oldest** — age in days of the oldest one.
  - **Prunable** — how many snapshots, counted from the oldest, are older than the
    "Older than \_\_ days" cutoff (settable, top right; defaults to 90) — stopping
    early at the first held snapshot, since pruning can't walk past a lock.
  - **Measured** — of the prunable snapshots specifically, what percent already have
    a cached size. Note the denominator is the *entire* tree history, not just the
    prunable subset, so this can read much lower than "how much of what's currently
    actionable is measured."
  - **Reclaim~** — bytes freed by the *measured* portion of the prunable prefix,
    stopping at the first unmeasured one. A floor, not an estimate — click into a
    tree and run Inspect to get the real, complete number.
  - **Refresh** (top right) — bypass the 5-minute snapshot-listing cache and re-fetch
    from the cluster right now.

### Tree detail (click a row on the Dashboard)

Two independent tables, each backed by its own on-demand background job (with live
per-item progress, since a single diff can be a large, slow operation against the
cluster):

- **Reclaim curve** — click **Inspect** (or **Re-inspect** to extend it after new
  snapshots exist). Produces the cumulative "delete everything older than this date"
  curve described above, with a **Delete** action per row.
- **Snapshot sizes** — click **Size snapshots**. Produces the per-snapshot
  "Individual size" table described above. Each row has its own **Delete**, and
  checking multiple rows reveals an **Estimate combined savings** action plus a
  **Delete selected** bulk action.

Both jobs are resumable (progress checkpoints to a local cache, safe to Stop and
restart), skip held snapshots by default, and isolate per-pair failures — one bad
diff doesn't cost you the results already computed for the rest of the tree.

### Admin (admin role only)

Create users, change roles, and activate/deactivate accounts. Any user can change
their own password (Layout header) with current-password verification.

## Architecture

Three containers via Docker Compose:

| Service | Image/build | Role |
|---|---|---|
| `frontend` | `./frontend` (Vite build → nginx) | Serves the React SPA, reverse-proxies `/api/` to `backend` (with SSE-friendly settings — buffering off, no timeout) |
| `backend` | `./backend` (Python 3.12) | FastAPI/uvicorn API + all snapshot-diff compute |
| `db` | `postgres:16-alpine` | App state: users, clusters, job history |

No Redis, no task queue — on-demand background jobs are plain `asyncio.create_task` +
a thread-pool executor for the sync Qumulo API calls, tracked in an in-process job
registry (`app/jobs.py`) and streamed to the browser over Server-Sent Events.

**Backend stack**: FastAPI, SQLAlchemy (async) + asyncpg, Alembic migrations, PyJWT,
`cryptography` (Fernet, for encrypting stored Qumulo tokens), httpx.

**Frontend stack**: React 19 + TypeScript, Vite, Tailwind CSS, `react-router-dom`.

**Two data stores, different purposes**:
- **Postgres** (`db` container) — application state: users, registered clusters
  (with encrypted tokens), and a durable history of background jobs
  (`app/models.py`).
- **SQLite cache** (`backend/app/qumulo/cache.py`, on the `snapman_cache` volume) —
  computed results, keyed by the cluster's own stable `cluster_name` (not the local
  DB row id) so two users who register the same physical cluster automatically share
  already-computed results instead of recomputing them. Holds the snapshot listing
  (5-minute TTL), resolved source paths, and every computed pairwise/three-way/multi-
  set diff result plus their resumable checkpoints.

**Auth & permissions**: httpOnly JWT cookies (never in `localStorage`). Three roles —
`admin` (everything, plus user management and visibility into all clusters),
`operator` (own clusters, can delete snapshots), `viewer` (own clusters, read + run
Inspect/Size snapshots, cannot delete). Permission is layered: the app role gates
whether the delete endpoint is reachable at all; independently, the *Qumulo token's*
own RBAC gates what actually executes on the cluster. The app never pre-validates a
token's cluster-side privileges — if a token lacks the needed privilege, the cluster
rejects the call and the app surfaces that error as-is.

**Compute layer** (`backend/app/qumulo/`): thin typed wrappers over the Qumulo
snapshot-diff REST APIs (`api.py`, `client.py`), then the actual math in
`compute/`:
- `snapshot_reclaim.py` — pairwise (two-snapshot) diff engine, powers the reclaim
  curve.
- `snapshot_exclusive.py` / `snapshot_exclusive_job.py` — three-way diff engine,
  powers per-snapshot Individual size.
- `deletion_estimate.py` — arbitrary-set diff engine, powers the combined-selection
  estimate.
- `curve.py`, `groups.py`, `reclaim.py`, `intervals.py` — supporting math (curve
  row grouping, prune-prefix/age logic, interval arithmetic).

## Running it

Prerequisites: Docker and Docker Compose.

```sh
git clone <this repo>
cd qumulo_snapman_v2
cp .env.example .env
```

Edit `.env` and fill in the required values:

```sh
# Password for the auto-created admin account.
ADMIN_PASSWORD=...

# Secret for signing session JWTs.
JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# Key for encrypting stored Qumulo tokens at rest.
TOKEN_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
```

`APP_PORT` (default `3003`) and `DB_PASSWORD` can be left at their defaults for a
local install.

```sh
docker compose up -d --build
```

Then open `http://localhost:3003` (or whatever `APP_PORT` you set), log in as
`admin` with the password you set, and add your first cluster from the Dashboard —
either a Qumulo bearer token (`qq auth_create_access_token`) or a
username/password, which the app exchanges for a session token server-side.

Database migrations run automatically on container start
(`alembic upgrade head`, see the backend `CMD`).

### HTTPS

Off by default (plain HTTP). To serve the web UI over HTTPS instead, set
`ENABLE_HTTPS=true` in `.env` and restart:

```sh
docker compose up -d
```

It still serves on the same `APP_PORT` — only the protocol changes, so
`https://localhost:3003` instead of `http://`; plain HTTP requests to that port
are rejected once HTTPS is on, not silently allowed alongside it.

To use your own certificate (recommended for anything customer-facing), place
`tls.crt` and `tls.key` in a `./certs` directory next to `docker-compose.yml`
*before* starting the container. If that directory is empty when
`ENABLE_HTTPS=true`, the frontend container generates a self-signed cert into
it automatically on first start (logged clearly to `docker compose logs
frontend`) and reuses that same cert on every subsequent restart — fine for
eval/internal use, but browsers will show a certificate warning until it's
replaced with one from a real CA. Note the self-signed cert (or the entrypoint
script itself) writes into `./certs` as root, so removing that directory later
may require `sudo` or an equivalent privileged cleanup.

## Development

```sh
# Backend tests (pure unit tests against a mock Qumulo API client, no cluster needed)
cd backend
pip install -e ".[dev]"
pytest test/ -v

# Frontend typecheck
cd frontend
npm install
npx tsc --noEmit
```

`backend/test/client.py` is an in-memory mock implementing the same `Client`
protocol as the real Qumulo REST client — register canned tree-diff/file-diff/attrs
responses, then drive the compute layer against it. All of the diff-math tests
(`test_snapshot_exclusive.py`, `test_deletion_estimate.py`, `test_run_inspect.py`,
`test_run_snapshot_exclusive.py`) use this pattern rather than talking to a real
cluster.

New database schema changes go through Alembic (`backend/alembic/versions/`); the
SQLite result cache self-migrates via `PRAGMA user_version` in `cache.py` and
generally doesn't need migrations for new tables.
