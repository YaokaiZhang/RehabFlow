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

From the repository root, after setting `LLM_KEY` (an Anthropic API key), run:

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

The run requires `LLM_KEY`: the evaluator backends generate with the Anthropic
API, like the product. The judge is `claude-opus-5-5` at `high` effort
(`--judge-model`, `--judge-effort`) and uses `LLM_JUDGE_KEY`, which defaults
to `LLM_KEY`. Full conversation capture is enabled by
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

## Langfuse

When the self-hosted Langfuse from `./start-dev.sh` is reachable, the runner
reports to it by default; `--no-langfuse` opts out and `--langfuse-port N`
matches a non-default port. `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` override the local development defaults. If Langfuse is
down, the run continues without it.

- Everything goes to the Langfuse environment `evaluation`, apart from normal
  development traces.
- Each case backend tags its turn traces `eval`, `eval-run:<run id>` and
  `eval-case:<suite>:<case id>`; filter by a case tag to see all its turns.
- Each case gets one `eval_case` trace with the same tags, placed in the
  case's first chat session. It holds every judge call (including repair
  attempts) as an `eval_judge` generation with prompt, output and token use.
- Scores on the `eval_case` trace: `rubric.<dimension>` (0-4, the judge's
  justification as the comment), `rubric.weighted_score`, `eval.passed` and
  `eval.judge_status`.

Open a low-scoring `eval_case` trace and follow its session to the turn traces
behind the grade.

## Corpus behavior

The cases cover Care Episode ownership; Patient Memory and Episode Memory scope
and lifecycle; triage summaries; long-context pruning and compaction; temporal
corrections; grounding and catalog boundaries; checkpoints; clarification and
resume; idempotency; urgent routing; reviewer repair; and uncertainty. Ten
frozen information-boundary cases cover deliberate no-call, Patient Memory,
selected-session retrieval, empty and failed retrieval recovery, current-session
compaction, Exercise Catalog, authorized web, and selected-session compaction
recall. All interactions are synthetic patient-style engineering fixtures.
