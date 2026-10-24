# Driver and operator interface

The research dashboard at `/` is unchanged: one stream, no accounts, built to show
live telemetry. This document covers the application at `/app`, which adds the
accounts, roles, sessions and history the system specification describes.

## Running it

```bash
uvicorn accident_risk.api.main:app --host 0.0.0.0 --port 8000
```

Then open <http://127.0.0.1:8000/app>. Use `--host 0.0.0.0` so a phone on the
same network can reach it; a single worker only, because inference state is held
in process memory.

A fresh database has no users, so nobody can sign in. Seed the first
administrator with an environment variable:

```bash
export ACCIDENT_RISK_BOOTSTRAP_ADMIN_PHONE="+971500000000"
export ACCIDENT_RISK_BOOTSTRAP_ADMIN_NAME="Your Name"
```

That runs only while the store is empty. Everyone after the first is created
from the Users screen.

| Setting | Default | Purpose |
| --- | --- | --- |
| `ACCIDENT_RISK_DATABASE` | `data/app.sqlite3` | Where accounts and sessions live |
| `ACCIDENT_RISK_BOOTSTRAP_ADMIN_PHONE` | unset | Seeds the first administrator |
| `ACCIDENT_RISK_EXPOSE_LOGIN_CODES` | `true` | Returns the login code in the API response |

## Signing in

Login is by mobile number plus a six-digit single-use code; there is no password.
**There is no SMS gateway.** While `ACCIDENT_RISK_EXPOSE_LOGIN_CODES` is true the
code comes back in the `/app/api/auth/request-code` response and the sign-in
screen prints it. That is how the pilot signs in, and it is why this build must
not be exposed beyond a trusted network. Set the variable to `false` and wire a
real gateway in `AuthService.request_code` before any wider deployment.

Codes expire after five minutes, are single-use, and lock out after five wrong
attempts. Tokens last twelve hours. Codes and tokens are stored as SHA-256
digests, never in clear text.

## Roles

Privilege is strictly ordered; each role inherits everything beneath it.

| Role | Adds |
| --- | --- |
| `driver` | Own connection status, session start/stop, own risk, own alerts and trips |
| `account_manager` | Live map of every driver, all alerts, all trips, driver enrolment |
| `administrator` | User accounts, device inventory, risk thresholds, access log |

A driver's requests for manager or administrator routes return `403`, and a
driver reading `/app/api/sessions` sees only their own rows.

## How a session records

1. The driver presses **Start session**. A row is created with a unique
   `stream_id`.
2. The device posts `SensorFrame` JSON to the existing `/ingest` endpoint using
   that `stream_id`. Nothing about ingestion changed.
3. `SessionRecorder` writes one track point per second and raises a `high_risk`
   alert when the fused index crosses the configured threshold, at most once a
   minute. It never raises into the ingestion path: a storage failure must not
   stop telemetry being processed.
4. **End session** computes the summary (duration, distance, top speed, idling,
   risk distribution, gaps) and stores it. A session whose longest gap exceeds
   the tolerance is flagged incomplete so it can be excluded from training.
5. A session with no frames for three minutes closes itself with reason
   `timeout`.

## Collision handling

`POST /app/api/driver/collision` raises a `pending` alert. The driver's screen
takes over with a twenty-second countdown; cancelling marks it `cancelled`, and
letting it run out marks it `confirmed`. Only the driver who owns the alert, or
a manager, can act on it.

## Caveats

- Thresholds change **presentation**, not the model. Moving them re-bands
  existing scores; it does not retrain anything.
- The risk index is uncalibrated. It is not a probability of a crash, and the
  interface says so on the driver's screen.
- Run a single worker. Inference state is in-process, and a second worker would
  hold a different synchronizer.
- The map needs internet for street tiles. Without it the interface says so and
  positions still appear in the list.
