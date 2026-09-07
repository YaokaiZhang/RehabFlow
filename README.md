# RehabFlow

RehabFlow is an episode-centered rehabilitation application for patient self-care
and clinician-supported recovery. A Care Episode keeps one concern's AI Triage
Intake, AI Daily Rehab, Professional Care, and durable Episode Memory together.

The product has three connected workflows:

- **AI Triage Intake** gathers symptoms, safety signals, goals, and missing
  context before recommending a next step.
- **AI Daily Rehab** uses database-backed exercises after the Rehab Safety Gate
  has been satisfied.
- **Professional Care** lets a patient and selected doctor share a Care
  Conversation, care plans, AI Care Summary, and optional live movement
  monitoring.

## Features and highlights

- **Episode-centered care context:** AI Triage Intake, AI Daily Rehab,
  Professional Care, and durable memory stay scoped to a specific Care Episode.
- **Authenticated context boundaries:** patient, episode, session, and source
  ownership are revalidated before protected context or interrupted state is
  loaded.
- **Durable AI workflow:** PostgreSQL-backed LangGraph checkpoints preserve
  clarification interrupts and safely resume the same authenticated session.
- **Bounded, authorized tool use:** the Consultant can access only explicitly
  allowed memory, exercise catalog, and web tools, with call budgets, timeouts,
  and recorded outcomes.
- **Independent release review:** Safety and Grounding Reviewers run in
  parallel; both must approve a response, with one bounded repair and complete
  re-review before release.
- **Idempotent delivery:** per-turn idempotency keys protect durable AI chat
  persistence from duplicate client submissions.
- **Reproducible evaluation:**  synthetic
  44 case live service-backed corpus cover authorization, memory scope,
  checkpoints, retrieval, reviewer behavior, and failure recovery.

## Architecture

```text
authenticated request
        |
context_integrity -- unauthorized/invalid --> unsafe_fallback
        |
      router -- urgent --> end
        |    \
 clarification      consultant
      |                |
   checkpoint       policy_gate
                           |
                 +---------+---------+
                 |                   |
          safety_reviewer    grounding_reviewer
                 \                   /
                    review_join
                 /       |        \
          safe_end   bounded repair  unsafe_fallback
```

## Quick start

After installing Docker and configuring provider credentials in `.env`, start the complete stack with one command:

```bash
./start-dev.sh
```

The launcher runs the PostgreSQL, Redis, Qdrant, FastAPI, and Product services through Docker Compose. It uses rootless Docker when the active context provides it. On networks where Docker Hub is unavailable, set `IMAGE_REGISTRY=docker.m.daocloud.io` before the command.

RehabFlow needs Python 3.10+, Node.js 20+, PostgreSQL, Redis, and either Qdrant
or an explicitly configured embedded Qdrant path.

```bash
cp .env.example .env
python3.10 -m venv backend/.venv
backend/.venv/bin/python -m pip install -r requirements.txt
npm ci
```

Configure the provider, database, checkpoint, Redis, Qdrant, JWT, and trace
settings in `.env`, apply the application and LangGraph checkpoint schemas,
then start the API and Product app:

```bash
cd backend
.venv/bin/alembic -c ../alembic.ini upgrade head
.venv/bin/python scripts/setup_langgraph_checkpoints.py
PYTHONPATH=. .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# In another shell, from the repository root:
npm run dev:product
```

Open <http://localhost:3000>. The API health endpoints are
<http://127.0.0.1:8000/health> and
<http://127.0.0.1:8000/ai/chat/health>.

For a one-command developer startup after the one-time dependency setup above,
run:

```bash
./start-dev.sh
```

The launcher starts PostgreSQL, Redis, and Qdrant with Docker Compose, applies
database migrations, initializes LangGraph checkpoints, and runs the API and
Product app. It first tries the current Docker context;
if the Docker socket requires elevated access, it uses your normal `sudo`
credential for Docker only. No permanent `docker` group change is required.

See [backend/README.md](backend/README.md) for required environment variables,
service ports, authentication, migrations, health probes, persistent
`systemctl --user` services, and evaluator usage.

## AI chat user flow

1. Register or sign in as a patient in the Product app.
2. Create or select a Care Episode and open its AI Triage Intake.
3. Submit a Care Episode-bound message. The browser proxy forwards the bearer
   token to `POST /ai/chat/{session_id}`; use `new` only for the first turn.
4. Consume the returned event envelope. A normal turn has `event: "response"`,
   the durable `session_id`, status, response text, and any safe source links.
   AI chat uses authenticated HTTP event responses, not an AI-chat WebSocket.
5. When status is `clarification_required`, show the returned clarification
   question and keep the same session ID.
6. Submit the patient's answer to that same session with a new idempotency key.
   The backend reauthorizes the patient and Care Episode, rehydrates the pending
   checkpoint, and resumes the interrupted turn.

The Product app is the user workflow. The Console app at
<http://localhost:3001> is an internal QA and diagnostic surface; it is not the
patient experience and must not be used to bypass authentication or Care
Episode ownership.

## Repository layout

- `apps/product`: patient and doctor product application
- `apps/console`: internal QA console
- `packages/shared`: shared browser API/auth/runtime helpers
- `backend/app`: FastAPI, LangGraph runtime, persistence, and services
- `backend/evals`: offline and service-backed engineering evaluations
- `docs/product`, `docs/adr`: product contract and architecture decisions

Docker Compose remains useful for local infrastructure. Complete application
image packaging, registry publishing, and production container deployment are
future production work, not requirements for this delivery.
