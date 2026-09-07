import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db.base import Base, TimestampMixin

AI_TURN_RECEIPT_STATUSES = ("running", "interrupted", "completed", "failed")


class Patient(Base, TimestampMixin):
	__tablename__ = "patients"

	patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	patient_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
	password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
	real_info: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
	subscription_tier: Mapped[str] = mapped_column(String(64), nullable=False, default="self_serve")

	ai_sessions: Mapped[list["AISession"]] = relationship(back_populates="patient", cascade="all, delete-orphan")
	care_episodes: Mapped[list["CareEpisode"]] = relationship(back_populates="patient", cascade="all, delete-orphan")


class Doctor(Base, TimestampMixin):
	__tablename__ = "doctors"

	doctor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	doctor_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
	password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
	real_info: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
	verification_status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending")


class PatientDoctorMapping(Base, TimestampMixin):
	__tablename__ = "patient_doctor_mappings"
	__table_args__ = (UniqueConstraint("patient_id", "doctor_id", name="uq_patient_doctor"),)

	mapping_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False
	)
	doctor_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("doctors.doctor_id", ondelete="CASCADE"), nullable=False
	)
	status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class CareEpisode(Base, TimestampMixin):
	__tablename__ = "care_episodes"

	care_episode_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False, index=True
	)
	issue_title: Mapped[str] = mapped_column(String(255), nullable=False)
	body_area: Mapped[str] = mapped_column(String(128), nullable=False)
	goal: Mapped[str] = mapped_column(String(255), nullable=False)
	short_description: Mapped[str] = mapped_column(Text, nullable=False)
	symptom_started_on: Mapped[date | None] = mapped_column(Date, nullable=True)
	origin: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
	safety_gate_status: Mapped[str] = mapped_column(String(64), nullable=False, default="needs_triage")
	status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
	selected_doctor_id: Mapped[uuid.UUID | None] = mapped_column(
		UUID(as_uuid=True), ForeignKey("doctors.doctor_id", ondelete="SET NULL"), nullable=True
	)
	latest_triage_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

	patient: Mapped[Patient] = relationship(back_populates="care_episodes")
	triage_summaries: Mapped[list["CareEpisodeTriageSummary"]] = relationship(
		back_populates="care_episode", cascade="all, delete-orphan"
	)
	triage_summary_drafts: Mapped[list["CareEpisodeTriageSummaryDraft"]] = relationship(
		back_populates="care_episode", cascade="all, delete-orphan"
	)


class CareEpisodeTriageSummary(Base):
	__tablename__ = "care_episode_triage_summaries"
	__table_args__ = (UniqueConstraint("care_episode_id", "version", name="uq_care_episode_triage_summary_version"),)

	triage_summary_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	care_episode_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_episodes.care_episode_id", ondelete="CASCADE"), nullable=False, index=True
	)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False, index=True
	)
	version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
	concern: Mapped[str] = mapped_column(String(255), nullable=False)
	relevant_context: Mapped[str] = mapped_column(Text, nullable=False)
	safety_signals: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	limitations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	recommendation: Mapped[str] = mapped_column(Text, nullable=False)
	missing_information: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	unresolved_questions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	clinician_review_needed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
	additional_context: Mapped[str] = mapped_column(Text, nullable=False, default="")
	source_ai_session_id: Mapped[uuid.UUID | None] = mapped_column(
		UUID(as_uuid=True), ForeignKey("ai_sessions.session_id", ondelete="SET NULL"), nullable=True
	)
	source_conversation_transcript: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

	care_episode: Mapped[CareEpisode] = relationship(back_populates="triage_summaries")

class CareEpisodeTriageSummaryDraft(Base):
	__tablename__ = "care_episode_triage_summary_drafts"

	triage_summary_draft_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	care_episode_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_episodes.care_episode_id", ondelete="CASCADE"), nullable=False, index=True
	)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False, index=True
	)
	concern: Mapped[str] = mapped_column(String(255), nullable=False)
	relevant_context: Mapped[str] = mapped_column(Text, nullable=False)
	safety_signals: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	limitations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	recommendation: Mapped[str] = mapped_column(Text, nullable=False)
	missing_information: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	unresolved_questions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	clinician_review_needed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
	additional_context: Mapped[str] = mapped_column(Text, nullable=False, default="")
	source_ai_session_id: Mapped[uuid.UUID | None] = mapped_column(
		UUID(as_uuid=True), ForeignKey("ai_sessions.session_id", ondelete="SET NULL"), nullable=True
	)
	source_conversation_transcript: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

	care_episode: Mapped[CareEpisode] = relationship(back_populates="triage_summary_drafts")

	@property
	def saved(self) -> bool:
		return False



class CareConnectionRequest(Base, TimestampMixin):
	__tablename__ = "care_connection_requests"
	__table_args__ = (UniqueConstraint("care_episode_id", "doctor_id", name="uq_care_episode_doctor_request"),)

	request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	care_episode_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_episodes.care_episode_id", ondelete="CASCADE"), nullable=False, index=True
	)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False, index=True
	)
	doctor_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("doctors.doctor_id", ondelete="CASCADE"), nullable=False, index=True
	)
	request_reason: Mapped[str] = mapped_column(Text, nullable=False)
	status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
	responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CareRelationship(Base, TimestampMixin):
	__tablename__ = "care_relationships"
	__table_args__ = (UniqueConstraint("care_episode_id", name="uq_care_relationship_episode"),)

	relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	care_episode_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_episodes.care_episode_id", ondelete="CASCADE"), nullable=False, index=True
	)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False, index=True
	)
	doctor_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("doctors.doctor_id", ondelete="CASCADE"), nullable=False, index=True
	)
	source_request_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_connection_requests.request_id", ondelete="CASCADE"), nullable=False
	)
	status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class CareConversationMessage(Base):
	__tablename__ = "care_conversation_messages"

	message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	care_episode_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_episodes.care_episode_id", ondelete="CASCADE"), nullable=False, index=True
	)
	relationship_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_relationships.relationship_id", ondelete="CASCADE"), nullable=False, index=True
	)
	sender_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
	sender_role: Mapped[str] = mapped_column(String(32), nullable=False)
	content: Mapped[str] = mapped_column(Text, nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AICareSummary(Base):
	__tablename__ = "ai_care_summaries"

	care_summary_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	care_episode_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_episodes.care_episode_id", ondelete="CASCADE"), nullable=False, index=True
	)
	relationship_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_relationships.relationship_id", ondelete="CASCADE"), nullable=False, index=True
	)
	conversation_digest: Mapped[str] = mapped_column(Text, nullable=False)
	plan_digest: Mapped[str] = mapped_column(Text, nullable=False)
	unresolved_questions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	raw_score_trend: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CareAppointment(Base, TimestampMixin):
	__tablename__ = "care_appointments"

	appointment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	relationship_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_relationships.relationship_id", ondelete="CASCADE"), nullable=False, index=True
	)
	care_episode_id: Mapped[uuid.UUID | None] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_episodes.care_episode_id", ondelete="SET NULL"), nullable=True, index=True
	)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False, index=True
	)
	doctor_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("doctors.doctor_id", ondelete="CASCADE"), nullable=False, index=True
	)
	scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
	scheduled_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
	status: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
	purpose: Mapped[str] = mapped_column(Text, nullable=False, default="")


class DoctorIntelligenceArtifact(Base, TimestampMixin):
	__tablename__ = "doctor_intelligence_artifacts"

	artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	doctor_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("doctors.doctor_id", ondelete="CASCADE"), nullable=False, index=True
	)
	artifact_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
	title: Mapped[str] = mapped_column(String(255), nullable=False)
	content: Mapped[str] = mapped_column(Text, nullable=False)
	sections: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	attention_map: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
	input_refs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
	visibility: Mapped[str] = mapped_column(String(64), nullable=False, default="doctor_private")
	version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
	status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class EpisodeRehabSession(Base, TimestampMixin):
	__tablename__ = "episode_rehab_sessions"

	session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	care_episode_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_episodes.care_episode_id", ondelete="CASCADE"), nullable=False, index=True
	)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False, index=True
	)
	recommended_exercises: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	checklist: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	patient_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
	session_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
	completion_status: Mapped[str] = mapped_column(String(32), nullable=False, default="in_progress")


class AIDailyRehabRecommendation(Base, TimestampMixin):
	__tablename__ = "ai_daily_rehab_recommendations"

	recommendation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	care_episode_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_episodes.care_episode_id", ondelete="CASCADE"), nullable=False, index=True
	)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False, index=True
	)
	source_triage_summary_id: Mapped[uuid.UUID | None] = mapped_column(
		UUID(as_uuid=True),
		ForeignKey("care_episode_triage_summaries.triage_summary_id", ondelete="SET NULL"),
		nullable=True,
		index=True,
	)
	source_triage_summary_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
	status: Mapped[str] = mapped_column(String(32), nullable=False, default="empty")
	empty_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
	query_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
	items: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AIDailyRehabList(Base, TimestampMixin):
	__tablename__ = "ai_daily_rehab_lists"
	__table_args__ = (UniqueConstraint("care_episode_id", name="uq_ai_daily_rehab_list_episode"),)

	list_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	care_episode_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_episodes.care_episode_id", ondelete="CASCADE"), nullable=False, index=True
	)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False, index=True
	)
	source_recommendation_id: Mapped[uuid.UUID | None] = mapped_column(
		UUID(as_uuid=True),
		ForeignKey("ai_daily_rehab_recommendations.recommendation_id", ondelete="SET NULL"),
		nullable=True,
	)
	items: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


class AISession(Base, TimestampMixin):
	__tablename__ = "ai_sessions"

	session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False
	)
	title: Mapped[str] = mapped_column(String(255), nullable=False, default="New Rehab Session")
	care_episode_id: Mapped[uuid.UUID | None] = mapped_column(
		UUID(as_uuid=True),
		ForeignKey("care_episodes.care_episode_id", ondelete="SET NULL"),
		nullable=True,
		index=True,
	)

	patient: Mapped[Patient] = relationship(back_populates="ai_sessions")
	messages: Mapped[list["AIMessage"]] = relationship(back_populates="session", cascade="all, delete-orphan")
	internal_messages: Mapped[list["AIInternalMessage"]] = relationship(back_populates="session", cascade="all, delete-orphan")

	def bind_care_episode(self, care_episode_id: uuid.UUID) -> None:
		if self.care_episode_id is not None and self.care_episode_id != care_episode_id:
			raise ValueError("AI session is already bound to a different Care Episode")
		self.care_episode_id = care_episode_id


class AIChatTurnReceipt(Base, TimestampMixin):
	__tablename__ = "ai_chat_turn_receipts"
	__table_args__ = (
		UniqueConstraint("session_id", "idempotency_key", name="uq_ai_chat_turn_receipts_session_idempotency_key"),
		CheckConstraint(
			"status IN ('running', 'interrupted', 'completed', 'failed')",
			name="ck_ai_chat_turn_receipts_status",
		),
		CheckConstraint("length(btrim(idempotency_key)) > 0", name="ck_ai_chat_turn_receipts_idempotency_key"),
		CheckConstraint("length(btrim(request_hash)) > 0", name="ck_ai_chat_turn_receipts_request_hash"),
	)

	id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	session_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("ai_sessions.session_id", ondelete="CASCADE"), nullable=False, index=True
	)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False, index=True
	)
	idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
	request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
	status: Mapped[str] = mapped_column(String(32), nullable=False)
	response_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
	workflow_version: Mapped[str] = mapped_column(String(128), nullable=False)

	def matches_request_hash(self, request_hash: str) -> bool:
		return self.request_hash == request_hash

	@property
	def replayable_response_payload(self) -> dict | None:
		if self.status in {"completed", "interrupted"}:
			return self.response_payload
		return None


class AIMessage(Base):
	__tablename__ = "ai_messages"

	message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	session_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("ai_sessions.session_id", ondelete="CASCADE"), nullable=False
	)
	sender_role: Mapped[str] = mapped_column(String(32), nullable=False)
	content: Mapped[str] = mapped_column(Text, nullable=False)
	message_metadata: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

	session: Mapped[AISession] = relationship(back_populates="messages")


def ai_message_ordering() -> tuple[Any, ...]:
	"""Order AI messages by their persisted metadata sequence."""
	return (
		AIMessage.message_metadata["sequence"].astext.cast(Integer).asc().nullslast(),
		AIMessage.message_id.asc(),
	)


class AIInternalMessage(Base):
	"""Durable internal model trace row with redacted input, output, and metadata."""
	__tablename__ = "ai_internal_messages"

	internal_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	session_id: Mapped[uuid.UUID | None] = mapped_column(
		UUID(as_uuid=True), ForeignKey("ai_sessions.session_id", ondelete="CASCADE"), nullable=True, index=True
	)
	patient_id: Mapped[uuid.UUID | None] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=True, index=True
	)
	agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
	event_type: Mapped[str] = mapped_column(String(64), nullable=False)
	content: Mapped[str] = mapped_column(Text, nullable=False, default="")
	model_input: Mapped[str] = mapped_column(Text, nullable=False, default="")
	event_metadata: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

	session: Mapped[AISession | None] = relationship(back_populates="internal_messages")


class MemoryDocument(Base, TimestampMixin):
	__tablename__ = "memory_documents"
	__table_args__ = (
		Index(
			"uq_memory_documents_patient_document",
			"patient_id",
			unique=True,
			postgresql_where=text("scope = $$patient$$ AND care_episode_id IS NULL"),
		),
		Index(
			"uq_memory_documents_episode_document",
			"care_episode_id",
			unique=True,
			postgresql_where=text("scope = $$episode$$ AND care_episode_id IS NOT NULL"),
		),
	)

	document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False, index=True
	)
	care_episode_id: Mapped[uuid.UUID | None] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_episodes.care_episode_id", ondelete="CASCADE"), nullable=True, index=True
	)
	compiled_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
	status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
	last_compiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
	editable_fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
	summary_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
	level1_keywords: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	level1_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
	level1_session_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
	level2_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
	last_memory_error: Mapped[str] = mapped_column(Text, nullable=False, default="")



class AISessionCompactionState(Base, TimestampMixin):
	__tablename__ = "ai_session_compaction_states"

	session_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("ai_sessions.session_id", ondelete="CASCADE"), primary_key=True
	)
	rolling_block: Mapped[str] = mapped_column(Text, nullable=False, default="")
	source_watermark: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
	last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
	attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class MemoryMaintenanceRun(Base, TimestampMixin):
	__tablename__ = "memory_maintenance_runs"
	__table_args__ = (
		UniqueConstraint("trigger_identity", name="uq_memory_maintenance_trigger_identity"),
	)

	maintenance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	trigger_identity: Mapped[str] = mapped_column(String(255), nullable=False)
	request_hash: Mapped[str] = mapped_column(String(128), nullable=False, default="")
	trigger: Mapped[str] = mapped_column(String(64), nullable=False)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False, index=True
	)
	care_episode_id: Mapped[uuid.UUID | None] = mapped_column(
		UUID(as_uuid=True), ForeignKey("care_episodes.care_episode_id", ondelete="CASCADE"), nullable=True, index=True
	)
	snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
	status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
	episode_status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
	patient_status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
	episode_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
	patient_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
	errors: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
	claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
	claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class PatientClinicalMemory(Base, TimestampMixin):
	__tablename__ = "patient_clinical_memory"

	memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False
	)
	fact_category: Mapped[str] = mapped_column(String(64), nullable=False)
	extracted_fact: Mapped[str] = mapped_column(Text, nullable=False)
	source_session_id: Mapped[uuid.UUID | None] = mapped_column(
		UUID(as_uuid=True), ForeignKey("ai_sessions.session_id", ondelete="SET NULL"), nullable=True
	)


def ai_internal_message_ordering() -> tuple[Any, ...]:
	return (
		AIInternalMessage.event_metadata["sequence"].astext.cast(Integer).asc().nullslast(),
		AIInternalMessage.internal_message_id.asc(),
	)


class ConsultationThread(Base, TimestampMixin):
	__tablename__ = "consultation_threads"

	thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False
	)
	doctor_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("doctors.doctor_id", ondelete="CASCADE"), nullable=False
	)
	status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")

	messages: Mapped[list["ConsultationMessage"]] = relationship(
		back_populates="thread", cascade="all, delete-orphan"
	)


class ConsultationMessage(Base):
	__tablename__ = "consultation_messages"

	message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	thread_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("consultation_threads.thread_id", ondelete="CASCADE"), nullable=False
	)
	sender_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
	content: Mapped[str] = mapped_column(Text, nullable=False)
	read_receipt: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

	thread: Mapped[ConsultationThread] = relationship(back_populates="messages")


class RehabPlan(Base, TimestampMixin):
	__tablename__ = "rehab_plans"

	plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False
	)
	doctor_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("doctors.doctor_id", ondelete="SET NULL"), nullable=True
	)
	state: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")

	sessions: Mapped[list["RehabSession"]] = relationship(back_populates="plan", cascade="all, delete-orphan")


class RehabSession(Base):
	__tablename__ = "rehab_sessions"

	session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	plan_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("rehab_plans.plan_id", ondelete="CASCADE"), nullable=False
	)
	scheduled_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
	completion_status: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
	updated_at: Mapped[datetime] = mapped_column(
		DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
	)

	plan: Mapped[RehabPlan] = relationship(back_populates="sessions")


class PatientClinicalRecord(Base):
	__tablename__ = "patient_clinical_records"

	record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	patient_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("patients.patient_id", ondelete="CASCADE"), nullable=False
	)
	generated_by_ai: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
	doctor_reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
		UUID(as_uuid=True), ForeignKey("doctors.doctor_id", ondelete="SET NULL"), nullable=True
	)
	detailed_content: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
	updated_time: Mapped[datetime] = mapped_column(
		DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
	)



class RehabExerciseVideo(Base, TimestampMixin):
	__tablename__ = "rehab_exercise_videos"

	exercise_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
	slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
	title: Mapped[str] = mapped_column(String(255), nullable=False)
	source_url: Mapped[str] = mapped_column(Text, nullable=False)
	video_url: Mapped[str] = mapped_column(Text, nullable=False)
	local_video_path: Mapped[str | None] = mapped_column(Text, nullable=True)
	public_video_url: Mapped[str | None] = mapped_column(Text, nullable=True)
	demo_profile: Mapped[str] = mapped_column(String(128), nullable=False, default="acl_knee_stiffness")
	relevance_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=999)
	relevance_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
	relevance_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
	download_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_downloaded")
	pose_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_extracted")
	exercise_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

	pose_extraction: Mapped["VideoPoseExtraction | None"] = relationship(
		back_populates="video", cascade="all, delete-orphan", uselist=False
	)


class VideoPoseExtraction(Base, TimestampMixin):
	__tablename__ = "video_pose_extractions"
	__table_args__ = (UniqueConstraint("exercise_id", name="uq_video_pose_extraction_exercise"),)

	extraction_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
	exercise_id: Mapped[uuid.UUID] = mapped_column(
		UUID(as_uuid=True), ForeignKey("rehab_exercise_videos.exercise_id", ondelete="CASCADE"), nullable=False
	)
	source_video_path: Mapped[str] = mapped_column(Text, nullable=False)
	frame_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
	detected_frame_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
	fps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
	duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
	detector_name: Mapped[str] = mapped_column(String(64), nullable=False, default="mediapipe_pose")
	detector_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
	pose_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
	metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

	video: Mapped[RehabExerciseVideo] = relationship(back_populates="pose_extraction")
