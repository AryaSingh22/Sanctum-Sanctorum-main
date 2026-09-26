# Notes: Sanctum Sanctorum Bookstore

## Live app

**https://sanctum-sanctorum-9u9j.onrender.com** (UI at `/`, API docs at `/docs`, health at `/health`)

- **First visit can take 30–60 seconds.** The free Render instance sleeps after ~15 idle minutes.
- No sign-up needed. On the **Members** tab, sign in with a seeded member ID:

  | ID | Member | Tier | Can do |
  |---|---|---|---|
  | 1 | Wong Li | supreme | everything, unlimited loans, 15% discount |
  | 2 | Christine Palmer | master | restricted books, 5 loans, 10% discount |
  | 3 | Jonathan Pangborn | adept | 3 loans, 5% discount |
  | 4 | Sara Lin | apprentice | 1 loan, no restricted books |

- Restricted books (e.g. *Darkhold*) need master or above; try one as member 4 to see the 403.

## Summary

- All five areas are done: books, members, orders, loans, stats/reports.
- **All 202 provided tests pass, unmodified.** I added 36 tests in four new files (238 total).
- All three optional extras are done: edge-case tests, safe concurrent orders for the last copy, and
  `GET /members` with pagination.
- The whole suite also passes against **PostgreSQL 16**, the database the deployment uses. Running it
  there found two Postgres-only bugs that SQLite hides (see "Deployment").

```
uv sync
uv run pytest            # 238 passed, SQLite in memory, no external services
```

## Architectural decisions and trade-offs

**Layering.** Routers only parse input, call one service function and return. Services hold every
business rule and raise domain errors (`NotFoundError`, `ForbiddenError`, `ConflictError` in
`app/errors.py`). One handler in `main.py` maps them to 404/403/409. Services never import FastAPI,
so the same rules could back a CLI or a background job. SPEC allows either this or `HTTPException`;
I converted in one refactor commit while all tests were green.

**Validation lives in the schemas.** Trimming, lengths, the ISBN-13 checksum, email normalization,
non-empty orders and repeated books are all checked before any database work. That is also what
makes 422 win over 404, which is the order SPEC requires.

**Stock is guarded by the database, not by Python.** Stock changes are a single conditional UPDATE:
`SET stock = stock - n WHERE id = ? AND stock >= n`. Zero rows changed means the copies are gone.
- *All-or-nothing orders:* lines are reserved one by one, and the first short line rolls back the
  ones already taken, so a failed order never leaves stock half-changed.
- *Lock order:* lines are reserved in book-id order, so two orders naming the same books in opposite
  order cannot deadlock (each UPDATE holds a row lock until commit).
- *Status changes use the same idea:* pay/cancel update only `WHERE status = 'pending'`, and returns
  only `WHERE returned_at IS NULL`. Exactly one of several simultaneous requests wins, so stock is
  restored once.

**Constraints as the last line of defence.** CHECK constraints keep stock and price ≥ 0. A partial
unique index (`member_id, book_id WHERE returned_at IS NULL`) allows one open loan per member and
book. Duplicate emails/ISBNs are checked up front for a clear message. `commit_or_conflict` turns
the unique-constraint backstop into a 409 when two identical requests race past that check (before
this, 7 of 8 simultaneous identical books failed with a 500).

**"Overdue" is defined once.** `Loan.is_overdue(now)` is a SQLAlchemy hybrid method: plain Python on
an instance, a SQL condition in a query. Loan status, the borrowing rules and member stats all use
it, so the strict "not overdue at exactly `due_at`" boundary cannot drift. Status is computed at
read time rather than stored, because a loan becomes overdue just by time passing.

**Money.** Integer cents throughout. Discounts use floor division. Late fees count started days with
`divmod` on timedeltas, so no floats anywhere. Order totals are `BigInteger`, because price × quantity
can exceed 32 bits (a $10,000 book × 3,000 copies).

**Queries.** Stats and top-books are aggregate queries (conditional counts, `GROUP BY`) rather than
loops in Python. Orders load all their books in one query. Trade-off: the member loan list filters
by status in Python. Those lists are small, and filtering on the same computed status that is
returned means the filter and the response cannot disagree.

**Pagination.** A generic `Page[T]` response and a `fetch_page` helper are shared by `/books` and
`/members`. This was extracted in its own refactor commit before the second use.

## Deployment

**Render (web service) + Neon (Postgres)**, both in Singapore. The config is in `render.yaml`.

- **Why Render:** the frontend calls the API with relative URLs, so API and UI ship as one service
  with no CORS or config. A container-style host runs `uvicorn` as-is. Vercel would need a
  serverless ASGI shim.
- **Why Neon:** SQLite on Render's free disk resets on every deploy/restart. Neon's free tier wakes
  automatically on the first connection, where some free tiers pause projects after inactivity.
- **Code changes for Postgres** (`app/db.py`):
  - map the `postgres://` / `postgresql://` URLs hosts hand out to the psycopg 3 driver
  - pass SQLite's `check_same_thread` only to SQLite
  - use `pool_pre_ping`, since Neon closes idle connections
- **"Don't add new dependencies":** the one exception is the Postgres driver, which deployment
  cannot avoid. It lives in a separate `postgres` dependency group that only the build installs
  (`uv export --group postgres`). `uv sync` and the test suite are unchanged and use SQLite alone.
- **Verified on real Postgres.** I ran the full suite against a local PostgreSQL 16 through a
  throwaway harness (not committed). It found two bugs SQLite cannot show:
  1. A **deadlock** between two orders for the same books in opposite order. Postgres locks rows,
     SQLite locks the whole file. Fixed with the book-id lock order.
  2. **32-bit overflow → 500** for large prices/stock and for large order totals. Fixed with
     `BigInteger` totals and a 422 above 2,147,483,647 for inputs.
- **Schema changes:** tables are created at startup with `create_all`, which never alters existing
  tables. There are no migrations, since Alembic would be a new dependency. Changing the schema
  means starting from a fresh database.

## Not finished / known limitations

- **Tier limit under simultaneous borrows.** One member borrowing two *different* books at the same
  instant can pass the loan limit (and the overdue check) twice. No simple constraint expresses
  "at most N per tier". With more time I would lock the member row (`SELECT … FOR UPDATE`) inside
  `create_loan`.
- **Pending orders reserve stock forever.** Nothing expires an unpaid order.
- **No authentication.** Any client can act as any member ID. The UI's "sign in" only picks an ID.
  This is out of scope for the spec, but it would come first in a real deployment.
- **No migrations** (see above).

## Spec points I found unclear or questionable

I found no test I believe is wrong. Where SPEC was silent, I chose the following and covered each
choice with a test in `tests/test_edge_cases.py`:

1. **Mixed-case title order** is left open. I sort case-insensitively (`lower(title)`), so SQLite
   and Postgres both give `apple, Banana, Cherry`.
2. **`min_price` > `max_price`** returns an empty page (200) rather than a 422.
3. **PATCH with an explicit `null`** is rejected with 422 (the existing schema's behaviour, kept),
   and an empty body changes nothing.
4. **Upper bounds on price, stock and quantity** are not specified. Values above 2,147,483,647
   return 422, so they never reach a column that cannot hold them.
5. **Questionable: late fees use the book's price at return time**, as specified. A price change
   during a loan changes the fee. The price at borrow time seems fairer.
6. **Questionable: sales and loans share one stock count.** Selling the last copy also blocks
   borrowing it, and vice versa. Separate library copies would match a real club.
7. **Questionable: pending orders hold stock indefinitely** (see limitations).
8. **Restricted status** is checked only when ordering or borrowing. Restricting a book later does
   not affect existing orders or loans.

## Tests I added

| File | What it covers |
|---|---|
| `tests/test_concurrency.py` | 8 threads racing, each with its own session on a file-based SQLite database: the last copy is sold/lent once; pay vs cancel; double cancel/return; duplicate ISBN/email; same book twice; opposite-order orders (the deadlock case). Each race test failed before its fix; the deadlock one only fails on Postgres, since SQLite locks the whole file. |
| `tests/test_data_integrity.py` | The database itself rejects negative stock/price and a second open loan of the same book. |
| `tests/test_edge_cases.py` | The spec choices above, literal `%`/`_` in search, all-or-nothing orders on 403 and 409, late fee at return-time price, error bodies, naive ISO datetimes, large numbers. |
| `tests/test_member_list.py` | `GET /members` pagination. |

## Git history

The assignment arrived as a zip, so there was no starter commit to keep. The first commit is the
unmodified starter, exactly as received. The second commit only moves it to the repository root,
because I first initialised Git one folder too high. After that, there is one commit per logical
change: the planted bug fixes first, then each feature, then hardening, with a race test proving
each concurrency bug before its fix.

## AI usage

I pair-programmed with **Claude Code** (in VS Code) throughout. It drafted designs and code; I
steered the order of work, reviewed every change at each checkpoint and decided what went in. Every
commit was reviewed by me before it was pushed.

What I used it for:
- reading the starter and the spec to list the planted bugs and missing pieces
- implementing features phase by phase
- writing the added tests
- debugging
- the Render/Neon deployment config and the Postgres verification harness

I worked in phases (planted bugs → features → hardening → deployment), stopping after each for review.

**Where the AI was wrong:**

1. **Stock oversold under concurrency.** Claude's first implementation of orders and loans read the
   stock into Python, checked it and wrote it back. It passed every provided test, but it was wrong.
   A race test showed **8 out of 8 simultaneous orders succeeding for a single copy**, while stock
   still read 0, hiding the oversell. Double cancels were as bad: eight cancels of one 3-copy order
   took stock from 7 to 31. I did not accept "the tests are green" as done. Each race was reproduced
   with a failing test first, and the logic was rewritten as conditional UPDATEs (commits `786af97`,
   `0db83bc`).
2. **Its fix could deadlock on Postgres.** The first version of that fix reserved each order's books
   in the order the customer listed them. That is fine on SQLite, but on Postgres two orders naming
   the same books in opposite order each lock one row and wait for the other, and Postgres kills one
   with a 500. It only showed up because we ran the full suite against a real PostgreSQL 16 instead
   of trusting SQLite. The fix was to reserve in book-id order (commit `8cb825f`).

The lesson I took: AI-written code that passes the given tests still needs its failure modes tested
on the real target, meaning concurrency and the production database.
