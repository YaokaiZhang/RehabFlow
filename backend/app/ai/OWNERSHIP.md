# AI runtime architecture

The RehabFlow AI package implements one constrained, authenticated LangGraph
workflow. The graph owns workflow control. Request authorization, SQL
transactions, context assembly, durable checkpoints, and response persistence
surround the graph at explicit service boundaries.

## Request and runtime boundary

`app.main` creates one `AgentRuntime` during FastAPI startup and attaches an
`AIChatTurnService` to the application. A chat turn follows this sequence:

1. `AIChatTurnService` validates the authenticated patient, AI Session, and
   Care Episode, then claims the database-backed idempotency receipt.
2. It loads authorized conversation, Patient Memory, Episode Memory, Triage
   Summary, and rehab-session sources.
3. `ContextAssembler` selects bounded, node-specific context packets and
   records their source identities.
4. `AgentRuntime` invokes the compiled graph with the session ID and
   authenticated owner bound into the LangGraph configuration.
5. The metadata-only checkpoint wrapper rehydrates protected content only
   inside the authorized invocation boundary.
6. The service persists the assistant response and internal events, completes
   the idempotency receipt, and removes completed checkpoint state.

`runtime.py` owns the long-lived compiled runtime. `graph_execution.py` owns
the service-oriented execution helper and construction of default runtime
dependencies. `rehab_graph.py` is the narrow graph facade for
`RehabGraphDeps`, `RehabGraphState`, dependency adaptation, graph
construction, and graph execution.

## Compiled graph

```text
START
  |
context_integrity
  | invalid
  +------------------------------> unsafe_fallback --> END
  |
router
  | urgent ----------------------> END
  | clarification ---------------> clarification --> interrupt/checkpoint
  | unsupported -----------------> unsafe_fallback --> END
  |
consultant
  |
policy_gate
  | hard stop -------------------> unsafe_fallback --> END
  |
  +-------------------+
  |                   |
safety_reviewer   grounding_reviewer
  |                   |
  +------ review_join-+
             |
             +-- approved ----------------------------> END
             +-- repairable --> consultant_repair --> policy_gate
             +-- stale context --> repair_revalidate_context
             |                       |
             |                       +-- consultant
             |                       +-- unsafe_fallback --> END
             +-- rejected/exhausted -----------------> unsafe_fallback --> END
```

`graph_construction.py` declares this topology and its finite route domains.
`graph_builder.py` binds concrete node functions, dependencies, and tracing to
that topology. `graph_routes.py` contains pure conditional-edge decisions and
does not mutate graph state.

## Graph node ownership

| Graph node | Owner | Responsibility |
| --- | --- | --- |
| `context_integrity` | `context_integrity.py` | Validate workflow identity, source scope, authorization flags, and context versions before model work. |
| `router` | `router.py` | Route urgent, clarification, unsupported, and Consultant requests using typed workflow state. |
| `clarification` | `clarification.py` | Sanitize the patient-facing question and create a durable LangGraph interrupt for the same session. |
| `consultant` | `consultant_invocation.py` | Plan bounded retrieval, invoke authorized tools, assemble model input, and produce the draft response. |
| `policy_gate` | `policy_gate.py` | Enforce deterministic authorization, version, deadline, call-budget, source, and process invariants. |
| `safety_reviewer` | `review_nodes.py` | Independently review the draft for safety and return a reducer-safe typed decision. |
| `grounding_reviewer` | `review_nodes.py` | Independently compare the draft with the authorized evidence manifest and return a typed decision. |
| `review_join` | `review_nodes.py` | Join both decisions and permit release only when the current review attempt is complete and approved. |
| `consultant_repair` | `repair.py` | Apply one policy-bounded repair instruction derived from reviewer feedback. |
| `repair_revalidate_context` | `repair.py` | Verify source and authorization versions before another Consultant pass. |
| `unsafe_fallback` | `fallback.py` | Produce the bounded terminal response for rejected or unavailable paths. |

## State and dependency ownership

| Boundary | Owner |
| --- | --- |
| Typed graph state, enums, reducers, reviewer decisions, and workflow version | `workflow_state.py` |
| Deterministic deadlines and model, tool, and repair budgets | `workflow_policy.py` |
| Provider, retrieval, catalog, reviewer, trace, checkpoint, and session dependencies | `runtime_dependencies.py` |
| Graph-specific dependency shape and runtime adaptation | `graph_dependencies.py` |
| Initial workflow control-state projection | `graph_state_projection.py` |
| Node prompt context and source-ordered context planning | `graph_prompt_context.py` |
| Per-turn evidence, context packets, Consultant snapshot, tool records, and web sources | `active_turn_context.py` |

`RehabGraphState` is a partial-update `TypedDict`. Identity, authorization,
route, budget, evidence metadata, reviewer decisions, and release status are
typed control-plane fields. User messages, context packets, tool results,
reviewer feedback, and drafts are content-bearing fields with separate
disclosure and persistence rules.

## Context and tool boundaries

| Boundary | Owner | Responsibility |
| --- | --- | --- |
| Initial context cards and bounded packets | `context_assembler.py`, `context_types.py`, `node_specs.py` | Convert authorized database sources into source-labeled, token-bounded packets for each node. |
| Progressive memory, catalog, and web tools | `consultant_tools.py` | Enforce the fixed tool schema, allowed tool names, episode/session scope, timeouts, and call budgets. |
| Tool-result compression | `tool_result_compression.py` | Preserve useful source content while bounding model input size. |
| Conversation compaction | `session_compaction.py` | Condense oversized active-session context while keeping the persisted conversation as source truth. |
| Evidence contracts and adapters | `evidence_types.py`, `evidence_registry.py`, `evidence_adapters.py` | Define authorized evidence requests/results and connect them to database and retrieval implementations. |
| Reliable web retrieval | `reliable_web_search.py` | Provide the bounded web-search implementation used by authorized tool dispatch. |
| Model input accounting | `model_input_budget.py` | Estimate and report model-message token use for bounded invocation. |

## Persistence, privacy, and observability

| Boundary | Owner | Responsibility |
| --- | --- | --- |
| PostgreSQL checkpoint lifecycle | `checkpointing.py` | Open the LangGraph saver, verify vendor tables, and clean completed threads. |
| Checkpoint content protection | `checkpoint_privacy.py` | Store metadata-only graph state, protect content references, bind access to the authenticated owner, and expose explicit authorized rehydration. |
| Internal AI event persistence | `graph_log_persistence.py` | Project and persist bounded internal events under the same patient and session ownership. |
| Runtime tracing | `graph_tracing.py` | Emit allowlisted node, route, timing, source, attempt, and outcome metadata. |
| Shared invocation utilities | `invocation_support.py` | Build bounded internal events, normalize model content, and sanitize provider failures. |

The public package surface in `app.ai.__init__` exposes
`AgentRuntime`, `AgentRuntimeDependencies`, `RehabGraphDeps`,
`RehabGraphState`, `WorkflowPolicy`, `build_rehab_graph`, and
`run_rehab_graph`. Internal code should import the concrete owner module for
the boundary it uses.
