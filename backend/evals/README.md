# Combined agent evaluation corpus

This directory contains versioned, synthetic, engineer-reviewed behavioral contracts for RehabFlow live service-backed engineering evaluation. These fixtures assert workflow paths, policy outcomes, authorization boundaries, memory and context information boundaries, checkpoint behavior, reviewer decisions, repair limits, and budgets. Tool-use quality is scored from persisted service runtime events and never acts as a hard gate.

## Canonical evaluator corpus

cases.yaml is the single active grouped corpus: exactly 36 longitudinal
service-backed cases. It contains human-editable titles, categories, concrete
prompts, information-boundary metadata, and response oracles. It contains no
active scripted tool or evidence outcomes. The loader validates every case
against the longitudinal schema and semantic contract, while corpus metadata
stays outside generation prompts.

The combined evaluator also reads exactly 8 real-model multi-turn cases from
real_model/cases/v2.json. combined/run.py is the only evaluator entrypoint and
the accepted run executes exactly 8 + 36 cases through isolated live service
processes with configured generation, memory, catalog, and web adapters. It
writes a structured JSON artifact plus a combined Markdown artifact containing
both suites. It also writes two readable Markdown sidecars:
`combined-<timestamp>-judge.md` contains scores, score bars, justifications,
reason codes, and privacy-safe evidence references; `combined-<timestamp>-messages.md`
contains human-readable AI messages and AI internal messages.

## Running the combined evaluator

From the repository root, with OPENAI_API_KEY already available, run:

    ./scripts/run_llm_judge_eval.sh --max-parallel-cases 2

The default report directory is backend/evals/artifacts/combined-v2.
Pass --artifact-dir to choose a different workspace-owned report directory.
The evaluator does not inspect Git or require a repository revision. It derives the current single Alembic head and a deterministic SHA-256 revision from backend/app plus scripts/dev-backend.mjs, unless an explicit evaluator override is supplied. It starts each evaluator backend through scripts/dev-backend.mjs, injects the isolated evaluator database and Qdrant resources, and verifies that every child reports the resolved backend revision. The shared database/setup.sh is for the normal developer database; the evaluator provisions its own database namespace so parallel cases do not use that shared database.

It requires OPENAI_API_KEY. OPENAI_JUDGE_API_KEY defaults to the generation key
when it is not set. Full conversation capture is enabled by default. Artifacts
include the complete input/output dialog, model and tool events, reviewer and
repair events, judge scores and rubric rationale, count cross-checks, and
explicit unavailable reasons.

Bound live evaluator API calls with either the environment variable or the
equivalent CLI option:

    REHAB_EVAL_MAX_PARALLEL_CASES=2 ./scripts/run_llm_judge_eval.sh
    ./scripts/run_llm_judge_eval.sh --max-parallel-cases 2

The default is 1. In service-backed mode, each case gets an isolated
short-lived backend process, so cases can run concurrently without sharing
case-specific catalog, Qdrant, fault, or restart state. The Markdown report
records the selected limit and includes judge score bars and justifications.
Use the `-judge.md` sidecar for a compact score review and the `-messages.md`
sidecar for readable persisted AI messages and AI internal messages.

For focused evaluator contract coverage:

    cd backend
    PYTHONPATH=. .venv/bin/python -m pytest tests/agent_eval -q

These tests do not replace the compulsory live 8+36 verification. A completed
accepted run requires the configured provider and judge, isolated databases and
Qdrant/catalog resources, service startup, and exact cleanup to succeed.

## Corpus behavior

The cases cover Care Episode ownership,
Patient Memory and Episode Memory scope/lifecycle, triage summaries, long
context pruning/compaction, temporal corrections, grounding/catalog
boundaries, checkpoints, clarification/resume, idempotency, urgent routing,
reviewer repair, uncertainty, and ten frozen information-boundary matrix
cases covering deliberate no-call, Patient Memory, selected-session, empty
and failed retrieval recovery, current-session compaction, Exercise Catalog,
authorized web, and selected-session compaction recall.

All interactions are synthetic patient-style engineering fixtures. They do not
contain real patient identifiers or secrets. Scripted model/tool outputs are
boundary tokens, not clinical prose.

