# RehabFlow backend

The backend is a FastAPI application with a constrained LangGraph workflow,
PostgreSQL application data and checkpoints, Redis admission state, and Qdrant
retrieval. Run commands below from the repository root unless a command starts
with `cd backend`.

## Prerequisites and ports

| Dependency | Default | Purpose |
| --- | --- | --- |
| Product app | `http://localhost:3000` | Patient and doctor product UI |
| Internal QA Console | `http://localhost:3001` | Engineering diagnostics |
| FastAPI | `http://127.0.0.1:8000` | Authenticated product API |
| PostgreSQL | `localhost:5432` | Application records and LangGraph checkpoints |
| Redis | `localhost:6379` | Single-use movement-stream tickets |
| Qdrant | `http://localhost:6333` | Authorized rehabilitation knowledge |

Use Python 3.10 or newer. The frontend workspaces require Node.js 20 or newer.
On hosts with multiple runtimes, verify `python --version`, `node --version`,
and `npm --version` before installation.

## Configuration

Copy `.env.example` to the repository-root `.env`. Process environment
values override the root file. `backend/.env` is a legacy fallback only.

Required runtime settings are grouped below. Never commit real credentials.

| Area | Variables |
| --- | --- |
| Application | `ENVIRONMENT`, `BACKEND_PORT`, `FRONTEND_ORIGIN` |
| Database | `DATABASE_URL`, `CHECKPOINT_DATABASE_URL`; if set, `POSTGRES_URL` must target the same database |
| Cache and retrieval | `REDIS_URL`; either `QDRANT_URL` or `QDRANT_PATH`; `QDRANT_COLLECTION_NAME` |
| Generation | `OPENAI_API_KEY`, `OPENAI_API_BASE`, `OPENAI_MODEL` |
| Embeddings | `EMBEDDING_PROVIDER`, `EMBEDDING_API_KEY`, `EMBEDDING_API_BASE`, `EMBEDDING_MODEL` |
| Security | unique `JWT_SECRET_KEY`, unique `REHAB_TRACE_HMAC_KEY`, `REHAB_TRACE_HMAC_KEY_VERSION` |
| Streaming | `STREAM_TICKET_TTL_SECONDS` (1-60 seconds) |
| Frontends | `PRODUCT_FRONTEND_PORT=3000`, `CONSOLE_FRONTEND_PORT=3001`, `BACKEND_PORT=8000` |

Production rejects default or shared JWT/provider/trace secrets. Evaluation has
additional isolated `REHAB_EVAL_*` settings; use the runner described below
instead of pointing evaluation at product databases.

## Install and initialize

The root `requirements.txt` delegates to the fully pinned
`backend/requirements.txt`.

```bash
python3.10 -m venv backend/.venv
backend/.venv/bin/python -m pip install --upgrade pip
backend/.venv/bin/python -m pip install -r requirements.txt
backend/.venv/bin/python -m pip install -r backend/requirements-dev.txt
npm ci
cp .env.example .env
```

Start PostgreSQL, Redis, and Qdrant using local services. `docker compose up -d`
may be used for infrastructure when Docker is available, but Docker is optional
for this delivery and complete application images are future production work.

The repository root also provides `./start-dev.sh`, which starts the complete
local development stack in one foreground process after dependencies and `.env`
are configured. It supports a rootless/current Docker context and falls back to
the invoking user access to sudo docker without requiring a permanent Docker
group change.

Application migrations and LangGraph vendor tables have separate owners:

```bash
cd backend
.venv/bin/alembic -c ../alembic.ini upgrade head
.venv/bin/python scripts/setup_langgraph_checkpoints.py
```

Do not run `alembic stamp` on an existing database until
`scripts/verify_schema_baseline.py` succeeds. API startup performs read-only
schema readiness checks; it does not create tables.

## Start and verify

```bash
cd backend
PYTHONPATH=. .venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8000
```

In separate shells:

```bash
npm run dev:product
npm run dev:console
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/ai/chat/health
```

Expected health responses report `status: "ok"`. The AI health response also
reports HTTP transport, workflow version, source revision, and evaluation
capabilities. A green health endpoint proves process readiness, not provider,
database fixture, or clinical correctness.

## Persistent development host with systemd

The foreground commands above are the portable local-development default. A
long-running Linux development host can run the same backend and Product app as
per-user systemd services:

- `rehabflow-backend.service`
- `rehabflow-product.service`

Create the following service files in ~/.config/systemd/user/. Replace every /absolute/path/to/... placeholder with the full path to your local RehabFlow checkout and installed runtime.
Point the backend unit at `backend/.venv/bin/python`. Point the Product unit
at the intended Node.js binary and `scripts/dev-next.mjs 3000`; do not assume
that a user systemd manager inherits an interactive shell or conda `PATH`.
Configure the Product unit with `After=rehabflow-backend.service` and
`Wants=rehabflow-backend.service`.

Backend unit example:

```ini
# ~/.config/systemd/user/rehabflow-backend.service
[Unit]
Description=RehabFlow backend API
After=default.target

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/rehab/backend
ExecStart=/absolute/path/to/rehab/backend/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --loop asyncio --http h11
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=3
KillMode=control-group

[Install]
WantedBy=default.target
```

Product unit example:

```ini
# ~/.config/systemd/user/rehabflow-product.service
[Unit]
Description=RehabFlow product frontend
After=rehabflow-backend.service
Wants=rehabflow-backend.service

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/rehab/apps/product
ExecStart=/absolute/path/to/node /absolute/path/to/rehab/scripts/dev-next.mjs 3000
Environment=PATH=/directory/containing/node:/usr/local/bin:/usr/bin:/bin
Environment=NEXT_TELEMETRY_DISABLED=1
Environment=BACKEND_PORT=8000
Environment=NEXT_PUBLIC_BACKEND_PORT=8000
Restart=always
RestartSec=3
KillMode=control-group

[Install]
WantedBy=default.target
```

After installing or changing either unit:

```bash
systemctl --user daemon-reload
systemctl --user enable --now \
  rehabflow-backend.service \
  rehabflow-product.service
```

Use systemd, rather than an additional shell process, to operate that host:

```bash
systemctl --user status rehabflow-backend.service rehabflow-product.service
systemctl --user restart rehabflow-backend.service rehabflow-product.service
journalctl --user -u rehabflow-backend.service -u rehabflow-product.service -f
```

Before restarting after a source update, install changed dependencies and apply
the application and checkpoint schemas:

```bash
backend/.venv/bin/python -m pip install -r requirements.txt
npm ci
cd backend
.venv/bin/alembic -c ../alembic.ini upgrade head
.venv/bin/python scripts/setup_langgraph_checkpoints.py
cd ..
systemctl --user restart rehabflow-backend.service rehabflow-product.service
```

For services to remain available after the SSH session ends, the host
administrator must enable user lingering once. Check it with
`loginctl show-user "$USER" -p Linger`.

These units run persistent development services, including the Next.js
development server; they are not production deployment definitions. After a
restart, verify `/health`, `/ai/chat/health`, and the Product app URL. If
behavior does not match the checked-out source, compare `systemctl --user
status` process paths and start times before starting another backend or
frontend manually.

## Authentication and AI chat

All durable AI chat requires a patient JWT. Obtain it through the Product app or
`POST /auth/register/patient` / `POST /auth/login`. The bearer identity must
own the requested Care Episode and existing AI session.

Create a session explicitly with `POST /ai/chat/session`, or send the first
turn to `POST /ai/chat/new`. Each turn body contains:

```json
{
  "message": "My knee is stiff after today's walk.",
  "care_episode_id": "<OWNED_CARE_EPISODE_UUID>",
  "idempotency_key": "<UNIQUE_VALUE_AT_LEAST_16_CHARACTERS>",
  "workflow_version": "2026-07-18"
}
```

The response is an HTTP event envelope. For ordinary turns, consume
`event`, `session_id`, `status`, `response`, and `sources`. If
`status` is `clarification_required`, render the response as the pending
question and submit the answer to the same session with a new idempotency key.
That second request is the resume boundary: authorization and checkpoint
ownership are checked again before content is rehydrated.

The app-owned `/ai-chat/{session_id}` proxy preserves the backend body and
Authorization header. AI chat has no product WebSocket. Movement streams are a
different feature and require a short-lived single-use ticket from
`/stream-tickets/patient-rehab` or
`/stream-tickets/doctor-monitor/{care_episode_id}`.

## Tests and evaluation

Provider-free checks do not require model credentials:

```bash
PYTHONPATH=backend backend/.venv/bin/python -m pytest \
  backend/tests/ai/test_graph_topology.py \
  backend/tests/ai/test_clarification_resume.py \
  backend/tests/observability/test_checkpoint_privacy.py \
  backend/tests/services/test_ai_turn_persistence.py -q

PYTHONPATH=backend backend/.venv/bin/python -m pytest backend/tests/agent_eval -q
```

The second command is the existing offline evaluator/contract suite. It uses
scripted adapters and fixtures and writes no model-provider artifacts.

To run the service-backed 44-case engineering diagnostic:

```bash
scripts/run_llm_judge_eval.sh \
  --artifact-dir evals/artifacts/combined-v2
```

Prerequisites are isolated evaluation PostgreSQL/application and checkpoint
databases, an isolated Qdrant namespace or path, catalog data, a generation
provider key, and a judge key (or the documented generation-key reuse). Run
`scripts/run_llm_judge_eval.sh --help` for concurrency, case, baseline, model,
and judge-thinking options. The default artifacts are JSON, Markdown, message,
judge, and ledger files under `backend/evals/artifacts/combined-v2`.
Successful runs retain their isolated resources by default; set
`REHAB_EVAL_KEEP_TEST_DATA=0` to clean runner-owned resources.

These evaluations are engineering regression evidence. They are not clinical
validation, a clinical safety claim, regulatory evidence, or proof that an
output is appropriate for an individual patient.
