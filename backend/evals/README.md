# Combined agent evaluation corpus

This directory contains versioned synthetic cases for live, service-backed RehabFlow evaluation. The cases check workflow paths, policy outcomes, memory and context boundaries, checkpoints, reviewer decisions, repair limits, and budgets. The evaluator scores tool use from persisted runtime events, but tool use is not a hard gate.

## Evaluator corpus

`cases.yaml` is the active grouped corpus. It contains 36 longitudinal
service-backed cases. It contains human-editable titles, categories, concrete
prompts, information-boundary metadata, and response oracles. It does not
contain active scripted tool or evidence outcomes. The loader checks each case
against the longitudinal schema and semantic contract, and keeps corpus metadata
out of generation prompts.

The combined evaluator also reads 8 real-model multi-turn cases from
`real_model/cases/v2.json`. `combined/run.py` is the only entrypoint. A run
executes all 8 + 36 cases through isolated live service
processes with configured generation, memory, catalog, and web adapters. It
writes a structured JSON artifact and a combined Markdown artifact for both
suites. It also writes two Markdown sidecars:
`combined-<timestamp>-judge.md` contains scores, score bars, justifications,
reason codes, and privacy-safe evidence references; `combined-<timestamp>-messages.md`
contains human-readable AI messages and AI internal messages.

## Running the combined evaluator

From the repository root, after setting `OPENAI_API_KEY`, run:

    ./scripts/run_llm_judge_eval.sh --max-parallel-cases 2

The default report directory is `backend/evals/artifacts/combined-v2`.
Use `--artifact-dir` to choose another workspace-owned directory. The evaluator
does not inspect Git or require a repository revision. Unless you provide an
override, it derives the current Alembic head and a deterministic SHA-256
revision from `backend/app` and `scripts/dev-backend.mjs`. It starts each
evaluator backend through `scripts/dev-backend.mjs`, injects isolated database
and Qdrant resources, and checks that every child reports the resolved backend
revision. `database/setup.sh` is for the normal developer database; the
evaluator provisions its own namespace so parallel cases do not share it.

The run requires `OPENAI_API_KEY`. If `OPENAI_JUDGE_API_KEY` is unset, the
generation key is used for judging. Full conversation capture is enabled by
default. Artifacts include the input and output dialog, model and tool events,
reviewer and repair events, judge scores and rubric rationale, count
cross-checks, and explicit unavailable reasons.

Set live evaluator concurrency with either the environment variable or the
equivalent CLI option:

    REHAB_EVAL_MAX_PARALLEL_CASES=2 ./scripts/run_llm_judge_eval.sh
    ./scripts/run_llm_judge_eval.sh --max-parallel-cases 2

The default is 1. In service-backed mode, each case gets an isolated short-lived
backend process. Cases can run concurrently without sharing case-specific
catalog, Qdrant, fault, or restart state. The Markdown report records the
selected limit and includes judge score bars and justifications.
Use the `-judge.md` sidecar for a compact score review and the `-messages.md`
sidecar for readable persisted AI and internal messages.

For focused evaluator contract coverage:

    cd backend
    PYTHONPATH=. .venv/bin/python -m pytest tests/agent_eval -q

These tests do not replace the live 8+36 verification. A completed run requires
the configured provider and judge, isolated databases and Qdrant/catalog
resources, successful service startup, and successful cleanup.

## Corpus behavior

The cases cover Care Episode ownership; Patient Memory and Episode Memory scope
and lifecycle; triage summaries; long-context pruning and compaction; temporal
corrections; grounding and catalog boundaries; checkpoints; clarification and
resume; idempotency; urgent routing; reviewer repair; and uncertainty. Ten
frozen information-boundary cases cover deliberate no-call, Patient Memory,
selected-session retrieval, empty and failed retrieval recovery, current-session
compaction, Exercise Catalog, authorized web, and selected-session compaction
recall. All interactions are synthetic patient-style engineering fixtures.
