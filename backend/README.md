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
| Langfuse | `http://localhost:3100` | Self-hosted LLM trace viewer |

Use Python 3.11 or newer (LangGraph needs 3.11+ to pass tracing callbacks into nested model calls). The frontend workspaces require Node.js 20 or newer.
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
| Generation (Anthropic) | `LLM_KEY` (required), `ANTHROPIC_MODEL` (default `claude-haiku-5-5`), `ANTHROPIC_EFFORT` (default `medium`), `ANTHROPIC_FALLBACKS` (default `true`), optional `ANTHROPIC_BASE_URL`, `ANTHROPIC_MAX_TOKENS`, `REHAB_WEB_SEARCH_TIMEOUT_SECONDS` (default `45`) |
| Embeddings | `EMBEDDING_PROVIDER`, `EMBEDDING_API_KEY`, `EMBEDDING_API_BASE`, `EMBEDDING_MODEL` |
| Security | unique `JWT_SECRET_KEY`, unique `REHAB_TRACE_HMAC_KEY`, `REHAB_TRACE_HMAC_KEY_VERSION` |
| Streaming | `STREAM_TICKET_TTL_SECONDS` (1-60 seconds) |
| Ports | Set on the start command (`--product-port`, `--backend-port`, ...), not in `.env`; see `./start-dev.sh --help` |

All chat/LLM calls (router, consultant, safety and grounding reviewers,
session compaction, memory agents, triage summary and allowlisted web search)
use Claude through the Anthropic API (`app/ai/anthropic_model.py`), by default
`claude-haiku-5-5`. Thinking is adaptive and on by default; `ANTHROPIC_EFFORT`
controls its depth and is the main latency/cost lever. Structured outputs use
native JSON-schema output. If Claude declines a request, the turn fails through
the normal model-failure path rather than returning an empty answer. Haiku has
no server-side refusal fallback; with an Opus or Sonnet model,
`ANTHROPIC_FALLBACKS=true` retries a declined request on another model first.
Web search uses Claude's `web_search` server tool restricted to the
reliable-domain allowlist. Embeddings still use `EMBEDDING_*` (OpenAI-compatible),
because Anthropic has no embeddings API.

Production rejects default or shared JWT/provider/trace secrets. Evaluation has
additional isolated `REHAB_EVAL_*` settings; use the runner described below
instead of pointing evaluation at product databases.

## Install and initialize

The root `requirements.txt` delegates to the fully pinned
`backend/requirements.txt`.

```bash
python3.11 -m venv backend/.venv
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

## LLM tracing with self-hosted Langfuse

For debugging prompts and agent behaviour, the backend sends every LangGraph
turn to a self-hosted [Langfuse](https://langfuse.com) instance. Each turn is
one `rehab_turn` trace, with a span per graph node and the exact prompts, model
outputs, latency and token usage of each LLM call nested under it. Traces are
grouped into Langfuse sessions by the RehabFlow chat session UUID, so a Langfuse
session lines up with the `ai_internal_messages` rows for that session.
Triage-summary generation is traced separately as `triage_summary`.
Allowlisted web search calls the Anthropic SDK directly and is recorded as a
`reliable_web_search` generation (prompt, response blocks, token usage and
search count) inside the graph node that ran it.

`./start-dev.sh` starts Langfuse by default; pass `--no-langfuse` to skip it
(the backend then sends no traces).

```bash
./start-dev.sh --no-langfuse
```

Langfuse comes from `docker-compose.langfuse.yml`: Langfuse web and worker
plus their own Postgres, ClickHouse, Redis and MinIO, all bound to
`127.0.0.1`. The first start pulls images and runs migrations, which can take a
few minutes. Open `http://localhost:3100` and log in with
`dev@rehabflow.local` / `rehabflow-dev`. The `rehabflow-dev` project and its
API keys are created automatically, and the Compose backend is pointed at it.
`./start-dev.sh shutdown` stops Langfuse too; its data stays in the
`langfuse_*` volumes.

To trace a backend running outside Compose, start Langfuse as above and set
`LANGFUSE_ENABLED=true`, `LANGFUSE_BASE_URL=http://localhost:3100`,
`LANGFUSE_PUBLIC_KEY=pk-lf-rehabflow-dev` and
`LANGFUSE_SECRET_KEY=sk-lf-rehabflow-dev` in `.env`.

Privacy rules:

- Langfuse stores full prompts and outputs, including patient messages. Use
  synthetic test patients, and keep the stack on localhost.
- The credentials in `docker-compose.langfuse.yml` are development defaults.
  Override the `LANGFUSE_*` variables listed there before using it anywhere
  shared.
- Tracing is off unless enabled, and the backend refuses it when
  `ENVIRONMENT=production` unless `LANGFUSE_ALLOW_PRODUCTION=true`.
- Exported text goes through the same secret redaction as internal graph logs.
  No patient or user ID is sent.
- Langfuse is a debugging tool. The privacy-safe `agent_trace` events and
  `ai_internal_messages` rows remain the system of record.

Tracing never blocks a turn: a missing package, bad configuration or an
unreachable Langfuse server only logs a warning.

### Prompt management in Langfuse

Every prompt the graph sends (router, consultant, retrieval planner, repair,
safety and grounding reviewers, memory agents, session compaction and triage
summary) is registered in `app/ai/prompt_defaults.py` under a Langfuse prompt
name and resolved through `app/ai/prompt_registry.py`.

1. Export the current prompts: `python scripts/export_langfuse_prompts.py`
   writes `backend/prompts/langfuse/<name>.txt` and an index README listing
   each prompt's `{{variables}}`.
2. Create each prompt in Langfuse as a **text** prompt with that name (paste
   the file), or upload them all as new versions with
   `python scripts/export_langfuse_prompts.py --upload --label staging`.
3. Set `LANGFUSE_PROMPTS_ENABLED=true`. The backend serves the version that
   carries `LANGFUSE_PROMPT_LABEL` (default `production`), cached for
   `LANGFUSE_PROMPT_CACHE_TTL_SECONDS` (default 60). Promote or roll back by
   moving that label to another version in Langfuse.

The in-code default is used whenever prompt management is off, Langfuse is
unreachable, the prompt or label does not exist, or a Langfuse version's
`{{variables}}` differ from the default's (a dropped variable would lose request
data, an unknown one would reach the model as literal braces). Missing prompts
are retried after the cache TTL, so prompts can be moved over one at a time.
Generations from the router, consultant, retrieval planner, repair, reviewers
and triage summary are linked to the prompt version in Langfuse traces.

These prompts decide clinical safety behaviour. Edit under a non-production
label, run the evals against it (`LANGFUSE_PROMPT_LABEL=staging`), then move
`production`. When you change a default in code, rerun the export script;
`scripts/test_prompt_registry_contract.py` fails if the exported files are stale.

The offline video pose-extraction scripts (`scripts/extract_video_poses.py`,
`scripts/extract_demo_video_pose_set.py`) need mediapipe, whose protobuf pin
conflicts with Langfuse's OpenTelemetry exporter. Install them into a separate
virtualenv from `backend/requirements-video.txt`.

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
databases, an isolated Qdrant namespace or path, and catalog data. Generation
uses the Anthropic API with `LLM_KEY`, like the product. The judge is
`claude-haiku-5-5` at `high` effort and uses `LLM_JUDGE_KEY`, which defaults to
`LLM_KEY`. Run `scripts/run_llm_judge_eval.sh --help` for concurrency, case,
baseline, judge model and judge effort options.

Eval runs report to Langfuse by default when it is reachable (start it with
`./start-dev.sh`; `--no-langfuse` opts out, `--langfuse-port N` matches a
non-default port). Filter by the Langfuse environment `evaluation` to keep eval
traces apart from development traces. Every case backend tags its turn traces
`eval`, `eval-run:<run id>` and `eval-case:<suite>:<case id>`. Each case also
gets an `eval_case` trace in its first chat session, holding the judge calls as
`eval_judge` generations and these scores: one `rubric.<dimension>` score per
rubric dimension (0-4, with the judge's justification as the comment),
`rubric.weighted_score`, `eval.passed` and `eval.judge_status`. The default artifacts are JSON, Markdown, message,
judge, and ledger files under `backend/evals/artifacts/combined-v2`.
Successful runs retain their isolated resources by default; set
`REHAB_EVAL_KEEP_TEST_DATA=0` to clean runner-owned resources.

These evaluations are engineering regression evidence. They are not clinical
validation, a clinical safety claim, regulatory evidence, or proof that an
output is appropriate for an individual patient.
