# Engineering highlights

RehabFlow gives the agent a staged way to find context and use it in a
rehabilitation response. This document describes the highlights of agent design.

## Contents

- [Agent architecture](#agent-architecture)
- [The context model](#the-context-model)
  - [Patient Memory](#patient-memory)
  - [Episode Memory](#episode-memory)
  - [Memory Documents](#memory-documents)
  - [Memory Context Index](#memory-context-index)
  - [Context interpretation and triage](#context-interpretation-and-triage)
- [How context is retrieved](#how-context-is-retrieved)
  - [Level 1: navigation](#level-1-navigation)
  - [Level 2: selected episode detail](#level-2-selected-episode-detail)
  - [Level 3: source conversation recall](#level-3-source-conversation-recall)
- [The consultant retrieval loop](#the-consultant-retrieval-loop)
  - [Tool roles](#tool-roles)
- [Agent roles and workflow](#agent-roles-and-workflow)
- [Why this design](#why-this-design)
- [Evaluation](#evaluation)

## Agent architecture

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


## The context model

RehabFlow keeps durable memory separate from the conversation that produced it.
The conversation is the source record. Memory is a maintained representation
that helps the agent find relevant parts of that record.

### Patient Memory

Patient Memory is the cross-episode layer. It contains stable facts that can
remain useful across rehabilitation concerns, including durable constraints,
preferences, recurring patterns, and long-running care context. It stays
concise, so a previous episode does not automatically become current evidence.

### Episode Memory

Episode Memory belongs to one Care Episode. It records the current concern,
saved triage summaries, rehabilitation progression, Professional Care notes,
AI Care Summaries, and unresolved questions. The Consultant uses this layer
for issue-specific answers.

### Memory Documents

The two layers live in Memory Documents, not in an ever-growing chat
transcript. Structured memory items are the editable source of truth. Compiled
summaries are views for patients, clinicians, and AI context. Episode and
Patient Memory Agents update the documents from saved, source-backed care
events. A current turn does not rewrite durable memory.

### Memory Context Index

The agent does not receive every memory item by default. A compiled Memory
Context Index points to relevant Patient Memory, Episode Memory, summaries,
transcripts, and care evidence. It tells the agent where to look while leaving
the source documents as the record.

### Context interpretation and triage

Context Interpretation groups source-backed candidates into themes and recall
hints. Context Triage ranks and labels them for the current workflow and node.
Neither layer creates clinical truth. They make relevant evidence findable and
keep its source identity and scope attached.

## How context is retrieved

Context assembly uses progressive disclosure. The initial Consultant context
contains the current session, compact Patient Memory, and Episode Memory Level
1. Other source cards, such as a saved Triage Summary, rehabilitation activity,
or authorized knowledge, go into node-specific packets with source labels and
token ceilings.

### Level 1: navigation

Episode Memory Level 1 contains keywords, a short description, and session IDs
that may contain relevant detail. It is an index, not the complete fact. Patient
Memory arrives as a concise cross-episode summary. Together, they let the agent
choose a retrieval path without injecting every historical conversation.

### Level 2: selected episode detail

When Level 1 points to a relevant session, the Consultant can request that
session's Level 2 Episode Memory entry. The tool returns the maintained detail
for that source. The session ID must appear in the active Level 1 index and
still belong to the patient and Care Episode.

### Level 3: source conversation recall

If Level 2 is empty or insufficient, the Consultant searches
that session with a compact query built from distinctive content terms. Each
call searches one authorized session. The agent checks the relevant indexed
candidates before it declares a remembered detail unavailable.

The three levels serve different purposes. Level 1 provides breadth, Level 2
provides a maintained detail view, and Level 3 reaches the original conversation
when that view is insufficient. Long-session compaction creates a bounded view
for context assembly; the persisted conversation remains the source record.

## The Consultant retrieval loop

The Consultant does not call every available tool for every question. It first
compares the user's request with the context already in the prompt and asks:

1. Is the answer already supported by the current session, Patient Memory, or
   Episode Memory Level 1?
2. If not, which source can provide the missing fact?
3. Does that source depend on another retrieval step?

This is the purpose of the retrieval plan. It turns the Consultant's information
need into a small, structured set of tool calls before any external or historical
data is fetched. The plan can also say that no retrieval is needed.

```text
current context + user request
              |
     retrieval-plan model call
              |
    action = none          action = retrieve
          |                         |
   draft answer       execute selected independent calls
                                    |
                         compress and label results
                                    |
                       next retrieval plan or answer
```

The planner returns a reason, an action, and a bounded list of calls. It chooses
`none` when the current context is enough. With `retrieve`, independent calls
can run together. Dependent calls wait for their prerequisites. For example,
the Consultant cannot search the original conversation until a Level 2 read
shows that the maintained Episode Memory entry is empty or insufficient.

The runtime checks each plan against the tools available for the current state,
rejects duplicate calls, checks arguments against the selected memory scope,
and compresses results before adding them to the next model input. The
Consultant then plans again if the returned evidence leaves another specific
gap; otherwise it writes the answer.

### Tool roles

- `read_episode_memory_level_2` retrieves one indexed Episode Memory detail.
- `search_session_conversation` recalls source messages from one selected
  session after Level 2 is insufficient.
- `rehab_exercise_kb_search` is the source for named exercises, instructions,
  dosage, progression, and catalog-backed rehabilitation recommendations.
- `web_search` is used for current public facts, precautions, contraindications,
  and evidence claims that need external sources; it is not the exercise catalog.

The tool descriptions encode information-need policy as well as function
signatures. They tell the model when a tool fits and when it must use another
source, so retrieval follows the question's meaning rather than a keyword.


## Agent roles and workflow

The system assigns different jobs to different agent roles.

```text
context preparation -> route the request -> Consultant retrieval and answer
                                                   |
                                  safety review + grounding review
                                                   |
                                  release, repair, or another pass
```

- The **Router** decides whether the request is urgent, needs clarification,
  or should enter consultation.
- The **Consultant** chooses retrieval, reads the returned evidence, and drafts
  the patient-facing answer.
- The **Safety Reviewer** checks the response for unsafe rehabilitation
  guidance and missed warning signals.
- The **Grounding Reviewer** compares claims with the authorized context,
  catalog records, and retrieval results.
- The **Episode Memory Agent** and **Patient Memory Agent** maintain durable
  memory outside the per-turn Consultant loop.
- The **Session Compaction Agent** creates bounded views of oversized active
  conversations without becoming a memory writer.

These roles handle different decisions. Routing is separate from retrieval,
retrieval is separate from memory maintenance, and the Consultant does not
decide whether its own draft is safe or supported. A reviewer disagreement sends
the response through one repair pass and then back through review.

When the Router or Consultant finds missing information, the workflow pauses
with a question and keeps the session context. The next answer resumes the
interrupted reasoning instead of starting with an empty prompt.

## Why this design

The design makes three tradeoffs.

The compact index and selected retrieval add orchestration compared with
injecting all history. In return, the agent has a visible path to the evidence
it used.

Separate Patient and Episode Memory scopes require maintenance logic. They also
keep a detail from one rehabilitation concern from becoming advice for another.

Planning retrieval adds a model decision before execution. It lets the system
record the information need, selected call, returned evidence, and resulting
answer as separate steps.

## Evaluation

The engineering corpus tests these choices as behaviors. Cases ask whether the agent:

- keeps Patient Memory and Episode Memory in their intended scopes;
- follows the Level 1 -> Level 2 -> Level 3 recall path;
- chooses the catalog for exercise-specific guidance and web retrieval for
  appropriate external facts;
- plans retrieval before execution and waits for dependent results;
- preserves qualifiers when synthesizing source conversation evidence; and
- separates Consultant drafting from safety and grounding review.

The evaluator combines 44 service-backed cases. The corpus and retained artifacts show the
context and tool behavior behind a result, not only an aggregate score. For more, see [backend/evals/README.md](../backend/evals/README.md)
