# RehabFlow Context

RehabFlow is an intelligent rehabilitation assistant for patient self-care and clinician-supported recovery. This glossary keeps product language consistent across patient, doctor, and AI-assisted care workflows.

## Language

**Professional Care**:
A shared doctor-patient care workspace for clinician-supported rehab, with role-specific patient and doctor views over the same care relationship. It includes communication, AI summaries, care plans, and optional live movement monitoring.
_Avoid_: Professional monitoring, doctor monitor dashboard, live monitor as the feature name

**Professional Care Subscription**:
The patient decision to opt into the Professional Care tier. In the MVP it is a one-click product state; in a complete product it is where payment or subscription management would live.
_Avoid_: Registration, payment step when no payment exists, upgrade as the canonical term

**Care Connection Request**:
A patient-initiated request for a specific doctor to join a Care Episode. It includes a short request reason; doctor acceptance makes the doctor selectable, and patient selection creates the active Care Relationship.
_Avoid_: Binding request, instant binding, doctor assignment when consent has not happened

**Care Relationship**:
The selected doctor-patient relationship for a Care Episode inside Professional Care. It is created after doctor acceptance and patient selection, and permits shared chat, care plans, summaries, and live monitoring access.
_Avoid_: Mapping as product language, subscription as relationship, request as relationship

**AI Care Summary**:
An AI-generated clinical collaboration summary for a Professional Care workspace. It may draw from doctor-patient communication, AI triage, rehab sessions, care plans, and unresolved follow-up questions.
_Avoid_: Score trend, telemetry chart, chat summary when broader care context is included

**Optional Live Movement Monitoring**:
A secondary Professional Care tool for observing movement during an active Care Relationship for a specific Care Episode. It supports care decisions, but it is not the Professional Care workspace or the primary summary of progress.
_Avoid_: Doctor monitor dashboard, patient-global monitor, score trend as the primary care surface, live stream as the feature name

**Doctor Expertise Tag**:
A patient-visible label on a doctor profile that helps match a Care Episode to relevant clinician expertise, such as body area, rehab context, or recovery goal. It supports search and filtering in the Doctor Selection Queue without replacing doctor verification or clinical judgment.
_Avoid_: Credential as tag, backend seed label, hidden metadata only

**Semantic Doctor Search**:
A Professional Care search experience that ranks doctors for a Care Episode by matching patient search text and episode context against doctor profile text and Doctor Expertise Tags. It supports patient choice in the Doctor Selection Queue, but it does not auto-assign a doctor or replace consent through Care Connection Requests.
_Avoid_: Doctor marketplace, automatic doctor assignment, exercise catalog search

**Doctor Selection Queue**:
The patient-facing Professional Care section that groups selected, accepted, and pending doctors for one Care Episode before showing additional doctors to request. It helps the patient choose a Care Relationship without treating the page as a full doctor directory.
_Avoid_: Doctor directory as the primary Professional Care screen, giant doctor cards, all doctors first

**Care Conversation**:
The persisted doctor-patient conversation inside an active Professional Care relationship. It supports care coordination and feeds AI Care Summary.
_Avoid_: Consultation thread as product language, live chat when realtime delivery is not guaranteed

**Care Worklist**:
The doctor-facing list of incoming Care Connection Requests, active Care Relationships, and patients needing follow-up inside Professional Care.
_Avoid_: Bind patient form as the primary doctor workflow, monitor dashboard as the doctor home

**Doctor Dashboard**:
The doctor-facing intelligence and visualization surface for a doctor panel across active Care Relationships. It can generate, refresh, revise, compare, and organize Doctor Intelligence Artifacts from Episode Memory Documents and current operational signals, while patient-care workflow execution stays in the Care Worklist.
_Avoid_: Doctor triage intake, episode-scoped dashboard, memory-only dashboard, Care Worklist action duplicate, prettier Care Worklist, renaming Care Worklist, patient AI Triage Intake for doctors


**Doctor Dashboard Assistant**:
The doctor-facing agent role inside Doctor Dashboard that generates, revises, compares, and organizes Doctor Intelligence Artifacts. It can read Professional Care visible Patient Memory items and Episode Memory Documents for active Care Relationships with source labels, but it is separate from the Memory Agent and does not write Memory Documents or execute patient-care workflow.
_Avoid_: Memory Agent, Care Worklist executor, doctor triage bot, patient-only memory, mixing patient-level background with episode-specific evidence


**Doctor Intelligence Artifact**:
A doctor-private, versioned insight, briefing, draft, comparison, or visualization created by Doctor Dashboard to help a doctor understand and plan work across active Care Relationships. Explicitly requested prep outputs can be saved as artifacts, but casual assistant answers are not artifacts by default, and artifacts do not execute care workflow by themselves.
_Avoid_: Patient-visible care record by default, Care Worklist action, sent message, committed care note, hidden automation, auto-saving every assistant answer


**Patient Panel Briefing**:
The primary Doctor Intelligence Artifact for Doctor Dashboard v1. It is a durable, versioned briefing for one doctor that synthesizes which active Care Relationships need attention, what changed since last review, upcoming Care Appointments, unresolved questions, rehab progress concerns, and suggested follow-up topics without becoming part of Memory Documents.
_Avoid_: Static metric dashboard as the primary artifact, duplicate Care Worklist cards, action queue, patient memory item, episode memory item


**Attention Map**:
A Doctor Dashboard visualization that groups active Care Relationships by synthesized attention reasons such as follow-up need, upcoming Care Appointment, stalled rehab, unresolved question, or stable status. It orients the doctor to the Patient Panel Briefing without becoming a Care Worklist action queue.
_Avoid_: Generic metric chart, duplicate active relationship cards, sortable task board


**Care Appointment**:
A scheduled Professional Care touchpoint that belongs to an active Care Relationship and may optionally point to a Care Episode. It is a coordination commitment between doctor and patient, not a triage action or memory item.
_Avoid_: Triage reminder, dashboard task as the appointment, unscheduled follow-up note

**AI Triage Intake**:
The patient-facing function that gathers symptoms, rehab stage, safety signals, and goals before routing the patient to a next care step.
_Avoid_: Chatbot as the feature name, knee intake when the scope is whole-body rehab

**AI Daily Rehab**:
The patient-facing function for guided rehab sessions and exercise feedback.
_Avoid_: Pose demo, score page, exercise tracker as the whole feature name

**AI Daily Rehab Recommendation**:
A patient-reviewed set of database-backed suggested exercises for a Care Episode, created from a saved Triage Summary or clinician-reviewed context. It is not an active rehab session until the patient starts it.
Saved Triage Summaries are the activation boundary for AI Daily Rehab Recommendations.
_Avoid_: Auto-created session, hidden schedule, triage plan when the patient has not reviewed it, AI-written exercise item with no database exercise

**AI Daily Rehab List**:
The reusable set of database-backed exercises a patient has chosen for a Care Episode. Daily rehab sessions can be started from this list, but each session has its own checklist and notes.
_Avoid_: Today-only checklist, hidden schedule, generated exercise text with no database exercise

**Exercise Catalog**:
The structured set of database-backed rehab exercises patients can search, filter, and add to an AI Daily Rehab List. It is the source of truth for exercise titles, descriptions, body structures, conditions, and media references shown in AI Daily Rehab.
_Avoid_: Free-text exercise list, Qdrant as the product catalog, AI-generated exercise catalog

**Care Paths**:
The retired post-login launch hub for AI Triage Intake, AI Daily Rehab, and Professional Care. It is legacy navigation language, not the product shell.
_Avoid_: Main menu, launch hub, current patient home

**Triage Summary**:
A precise, readable, and complete handoff from AI Triage Intake. It captures the patient concern, relevant context, safety signals, unanswered questions, and recommended next step for AI Daily Rehab or Professional Care.
_Avoid_: Chat transcript, vague route label, partial symptom note

**Clinician Review Recommendation**:
A patient-facing caution in a Triage Summary that suggests involving a clinician even when the Rehab Safety Gate has passed. It does not by itself block AI Daily Rehab Recommendations.
_Avoid_: Safety gate failure, extra safety review, blocked AI Daily Rehab when triage safety routing passed

**Triage Summary Request**:
The patient action that asks RehabFlow to generate a backend structured Triage Summary when the patient is done with intake. It is separate from ordinary triage chat turns.
_Avoid_: Automatic summary when the patient has not requested one, frontend-only summary generation

**Professional Care Subscriber**:
A patient who has opted into Professional Care through a Professional Care Subscription. A subscriber may still be waiting for doctors to accept Care Connection Requests or may need to select one accepted doctor for a Care Episode.
_Avoid_: Connected patient when no Care Relationship exists, paying patient in the MVP

**Care Episode**:
A specific rehab concern or recovery issue that groups related triage sessions, rehab sessions, Professional Care activity, memory, and context. A Care Episode has at most one selected doctor at a time, even if multiple doctors accepted connection requests for that episode.
_Avoid_: Global patient-doctor ownership, multiple bonded doctors for the same issue


**Care Episode Brief**:
A concise, role-appropriate snapshot of the patient situation for one Care Episode, including the issue, goal, safety context, Triage Summary, rehab activity, Professional Care status, and unresolved questions. It helps patients and doctors understand the episode before acting, but it is not a separate care function.
_Avoid_: Patient situation as vague product language, raw UUID card, live monitoring summary

**Patient Dashboard**:
The logged-in patient home that shows cross-episode attention, recent rehab activity, Professional Care status, and the Care Episode List. It orients the patient across rehab concerns without replacing episode-scoped work.
_Avoid_: Care Paths, marketing homepage, one giant episode, Patient Memory as primary navigation

**Episode Dashboard**:
The patient-facing workspace for one Care Episode. It summarizes the episode state and routes the patient into AI Triage Intake, AI Daily Rehab, Professional Care, and History for that specific concern.
_Avoid_: Patient Dashboard, Care Paths, generic details page, mixing multiple Care Episodes

**Care Episode List**:
The patient-visible list of active and past Care Episodes inside the Patient Dashboard. It lets the patient create or return to the right rehab concern before starting triage, rehab sessions, or Professional Care work.
_Avoid_: Treating each triage as a separate issue by default, logged-in home by itself, global patient memory with no episode boundary

**Episode Memory**:
The durable context for one Care Episode, built from its triage sessions, rehab sessions, Professional Care communication, care plans, and AI summaries.
_Avoid_: Global memory when the context belongs to one issue, chat history as the only memory

**Patient Memory**:
The durable cross-episode context for a patient, including stable identity, long-running rehab constraints, preferences, clinician relationships, and patterns that remain relevant across Care Episodes. It complements Episode Memory without replacing the episode boundary for issue-specific care.
_Avoid_: Treating every past issue as relevant to the current episode, chat history as the only memory, patient-global doctor ownership

**Memory Document**:
A maintained clinical-context document that stores durable rehab memory separately from chat turns. Its structured memory items are the editable source of truth, while prose views are compiled for patient review and AI context.
_Avoid_: Prompt-stuffed transcript, raw event dump, chat context as the memory store, one giant editable blob as canonical memory

**Memory Context Index**:
An internal, compiled guide that helps AI-assisted workflows find relevant Patient Memory, Episode Memory, transcripts, summaries, and care evidence without exposing every source in the prompt by default. It is derived from Memory Documents and source records; it is not a patient-authored memory item or a replacement for the Memory Document source of truth.
_Avoid_: Patient-editable memory item, hidden clinical fact, raw transcript dump, one giant prompt blob

**Context Interpretation Artifact**:
An internal, versioned interpretation of source-backed care context that groups evidence, labels themes, summarizes relevant history, and suggests recall hints for AI-assisted workflows. It is an agent navigation aid, not a Memory Document item, patient-authored memory, or source of clinical truth.
_Avoid_: Memory Document item, patient-visible fact by default, unsourced summary, hidden care record

**Context Integrity Layer**:
The internal boundary that enforces source identity, access scope, visibility, lifecycle state, citation, and context budget rules before AI-assisted workflows receive care context. It protects context assembly without trying to infer clinical meaning from prose.
_Avoid_: Clinical judgment engine, safety reviewer, memory author, keyword diagnosis rules

**Context Triage**:
An internal AI-assisted context preparation step that ranks, groups, labels, and summarizes source-backed context candidates for the current AI workflow. It decides relevance and recall needs, but it does not create Memory Document source truth or replace the Safety Reviewer.
_Avoid_: User-facing triage intake, Memory Agent, source of truth, final safety gate

**Patient Memory Document**:
The patient-editable Memory Document for stable cross-episode patient context, including durable constraints, preferences, recurring patterns, and long-running care considerations. Its memory items carry visibility such as patient-only, AI-assistant context, or Professional Care visible, and the patient can review, edit, delete, or adjust visibility for them.
_Avoid_: Global care plan, all past episodes as current evidence, patient profile as the whole memory, hidden memory the patient cannot inspect, all patient memory visible to doctors by default

**Episode Memory Document**:
The system-managed Memory Document for one Care Episode, including the current concern, saved Triage Summary history, rehab progression, Professional Care notes, AI Care Summaries, unresolved questions, and current next-step state. It incorporates sourced doctor notes without giving doctors or patients direct document rewrite ownership.
_Avoid_: Patient-wide memory, raw session list, regenerated summary with no durable source, patient-edited care record, doctor-edited memory blob

**Memory Agent**:
An umbrella term for conservative agents that maintain durable Memory Documents. It is specialized into an Episode Memory Agent and a Patient Memory Agent, which make evidence-backed and reversible changes after saved Triage Summaries or during appropriate maintenance work.
_Avoid_: Session Compaction Agent, per-turn memory writer, hidden always-on observer, chat agent as the memory owner

**Session Compaction Agent**:
An internal agent that condenses an oversized Care Conversation into a bounded context view for AI-assisted work while the original conversation remains the source of truth. It does not create or edit Patient Memory or Episode Memory.
_Avoid_: Memory Document source of truth, durable patient fact, replacement for the conversation record, Memory Agent

**Episode Memory Agent**:
The durable maintainer for one Care Episode's Episode Memory Document. It reviews each saved Triage Summary for episode-specific context, maintains a keyword-and-description view and a more detailed summary, and preserves the evidence and reason for any change.
_Avoid_: Patient Memory Agent, cross-episode patient fact, raw transcript compressor, doctor-edited memory blob

**Patient Memory Agent**:
The durable maintainer for a patient's Patient Memory Document. It reviews each saved Triage Summary for stable cross-episode context, maintains one substantially more concise summary than Episode Memory, and excludes facts that belong only to the active Care Episode.
_Avoid_: Episode Memory Agent, all past episodes as current evidence, raw transcript compressor, hidden patient profile

**Manual Care Episode**:
A Care Episode created by the patient without first completing AI Triage Intake. It records the patient-stated issue, body area, goal, and description, but does not by itself clear the patient for AI Daily Rehab.
_Avoid_: Manual episode as safety clearance, unscreened self-rehab context

**Rehab Safety Gate**:
The requirement that AI-assisted self-rehab uses AI Triage Intake or equivalent clinician-reviewed context before exercise guidance begins.
_Avoid_: Starting AI Daily Rehab from an unscreened issue

**Workflow Checkpoint**:
The durable internal state of an interrupted AI-assisted workflow that allows an authorized patient request to resume after clarification or another explicit pause. It records only the control state needed for safe continuation, is bound to the AI Session and Care Episode where applicable, and is revalidated before use.
_Avoid_: Chat transcript as checkpoint, Patient Memory, Episode Memory, unauthenticated session continuation, permanent copy of every prompt

**Grounding Reviewer**:
The internal agent role that checks whether an AI consultant draft is supported by authorized context and approved evidence, including Exercise Catalog or retrieval results. It runs independently from the Safety Reviewer, does not create source truth, and cannot release or rewrite a response by itself.
_Avoid_: Safety Reviewer, Context Integrity Layer, Context Triage, citation formatter, self-approving consultant
