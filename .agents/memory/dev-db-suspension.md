---
name: Dev DB suspension causes 500s
description: How to recognize that a 500 (e.g. Google OAuth callback) is a suspended development database, not an app bug.
---

# Dev database suspension shows up as a 500, not a clear DB error

When a DB-backed route returns HTTP 500 — the Google sign-in callback
(`/google_login/callback`) is a common one — first check whether the
**development database itself is reachable** before treating it as a code bug.

The Replit-managed Postgres (Neon) dev endpoint can be **suspended after
inactivity**. While suspended, every query fails with:

```
psycopg2.OperationalError: ... the endpoint has been disabled. Enable it using the API and retry.
```

The OAuth callback in `google_auth.py` catches `SQLAlchemyError` and calls
`abort(500)`, so the user-visible symptom (a 500 on login) hides the real cause
(database outage). Other DB-backed pages fail too — login just happens to be a
frequently hit one.

**How to apply / fix:**
- Confirm the cause with `checkDatabase()` and a trivial `executeSql("SELECT 1")`.
  A "not provisioned" result or the "endpoint has been disabled" error in logs
  confirms it.
- `createDatabase()` may report the DB "already exists" without re-enabling the
  compute endpoint, so it does NOT auto-fix a suspended endpoint.
- The reliable fix is to re-enable the existing database from the Replit
  **Database** tool (keeps all data). Creating a fresh DB works but starts empty.
- After it's back, restart the `Production Server` workflow to clear the stale
  (broken) SQLAlchemy connection pool, then verify a DB-backed page loads.

**Why:** Saved >1 debugging cycle — the 500 looked like an OAuth bug but the
code was correct; the dev DB had been suspended. These dev DBs can suspend again.
