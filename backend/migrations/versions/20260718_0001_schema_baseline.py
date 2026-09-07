"""Create the static RehabFlow application-schema baseline.

This revision intentionally creates only RehabFlow application tables. LangGraph
checkpointer/vendor tables are provisioned separately by deployment tooling.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260718_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UPGRADE_SQL = ('CREATE TABLE doctors (\n'
 '\tdoctor_id UUID NOT NULL, \n'
 '\tdoctor_name VARCHAR(255) NOT NULL, \n'
 '\tpassword_hash VARCHAR(512) NOT NULL, \n'
 '\treal_info JSONB NOT NULL, \n'
 '\tverification_status VARCHAR(64) NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (doctor_id), \n'
 '\tUNIQUE (doctor_name)\n'
 ')',
 'CREATE TABLE patients (\n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tpatient_name VARCHAR(255) NOT NULL, \n'
 '\tpassword_hash VARCHAR(512) NOT NULL, \n'
 '\treal_info JSONB NOT NULL, \n'
 '\tsubscription_tier VARCHAR(64) NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (patient_id), \n'
 '\tUNIQUE (patient_name)\n'
 ')',
 'CREATE TABLE rehab_exercise_videos (\n'
 '\texercise_id UUID NOT NULL, \n'
 '\tslug VARCHAR(255) NOT NULL, \n'
 '\ttitle VARCHAR(255) NOT NULL, \n'
 '\tsource_url TEXT NOT NULL, \n'
 '\tvideo_url TEXT NOT NULL, \n'
 '\tlocal_video_path TEXT, \n'
 '\tpublic_video_url TEXT, \n'
 '\tdemo_profile VARCHAR(128) NOT NULL, \n'
 '\trelevance_rank INTEGER NOT NULL, \n'
 '\trelevance_score INTEGER NOT NULL, \n'
 '\trelevance_notes TEXT NOT NULL, \n'
 '\tdownload_status VARCHAR(32) NOT NULL, \n'
 '\tpose_status VARCHAR(32) NOT NULL, \n'
 '\texercise_metadata JSONB NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (exercise_id)\n'
 ')',
 'CREATE TABLE ai_sessions (\n'
 '\tsession_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\ttitle VARCHAR(255) NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (session_id), \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE care_episodes (\n'
 '\tcare_episode_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tissue_title VARCHAR(255) NOT NULL, \n'
 '\tbody_area VARCHAR(128) NOT NULL, \n'
 '\tgoal VARCHAR(255) NOT NULL, \n'
 '\tshort_description TEXT NOT NULL, \n'
 '\tsymptom_started_on DATE, \n'
 '\torigin VARCHAR(32) NOT NULL, \n'
 '\tsafety_gate_status VARCHAR(64) NOT NULL, \n'
 '\tstatus VARCHAR(32) NOT NULL, \n'
 '\tselected_doctor_id UUID, \n'
 '\tlatest_triage_summary JSONB, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (care_episode_id), \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(selected_doctor_id) REFERENCES doctors (doctor_id) ON DELETE SET NULL\n'
 ')',
 'CREATE TABLE consultation_threads (\n'
 '\tthread_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tdoctor_id UUID NOT NULL, \n'
 '\tstatus VARCHAR(32) NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (thread_id), \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(doctor_id) REFERENCES doctors (doctor_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE doctor_intelligence_artifacts (\n'
 '\tartifact_id UUID NOT NULL, \n'
 '\tdoctor_id UUID NOT NULL, \n'
 '\tartifact_type VARCHAR(64) NOT NULL, \n'
 '\ttitle VARCHAR(255) NOT NULL, \n'
 '\tcontent TEXT NOT NULL, \n'
 '\tsections JSONB NOT NULL, \n'
 '\tattention_map JSONB NOT NULL, \n'
 '\tinput_refs JSONB NOT NULL, \n'
 '\tvisibility VARCHAR(64) NOT NULL, \n'
 '\tversion INTEGER NOT NULL, \n'
 '\tstatus VARCHAR(32) NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (artifact_id), \n'
 '\tFOREIGN KEY(doctor_id) REFERENCES doctors (doctor_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE patient_clinical_records (\n'
 '\trecord_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tgenerated_by_ai BOOLEAN NOT NULL, \n'
 '\tdoctor_reviewer_id UUID, \n'
 '\tdetailed_content JSONB NOT NULL, \n'
 '\tupdated_time TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (record_id), \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(doctor_reviewer_id) REFERENCES doctors (doctor_id) ON DELETE SET NULL\n'
 ')',
 'CREATE TABLE patient_doctor_mappings (\n'
 '\tmapping_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tdoctor_id UUID NOT NULL, \n'
 '\tstatus VARCHAR(32) NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (mapping_id), \n'
 '\tCONSTRAINT uq_patient_doctor UNIQUE (patient_id, doctor_id), \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(doctor_id) REFERENCES doctors (doctor_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE rehab_plans (\n'
 '\tplan_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tdoctor_id UUID, \n'
 '\tstate VARCHAR(32) NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (plan_id), \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(doctor_id) REFERENCES doctors (doctor_id) ON DELETE SET NULL\n'
 ')',
 'CREATE TABLE video_pose_extractions (\n'
 '\textraction_id UUID NOT NULL, \n'
 '\texercise_id UUID NOT NULL, \n'
 '\tsource_video_path TEXT NOT NULL, \n'
 '\tframe_count INTEGER NOT NULL, \n'
 '\tdetected_frame_count INTEGER NOT NULL, \n'
 '\tfps FLOAT NOT NULL, \n'
 '\tduration_seconds FLOAT NOT NULL, \n'
 '\tdetector_name VARCHAR(64) NOT NULL, \n'
 '\tdetector_version VARCHAR(64) NOT NULL, \n'
 '\tpose_data JSONB NOT NULL, \n'
 '\tmetrics JSONB NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (extraction_id), \n'
 '\tCONSTRAINT uq_video_pose_extraction_exercise UNIQUE (exercise_id), \n'
 '\tFOREIGN KEY(exercise_id) REFERENCES rehab_exercise_videos (exercise_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE ai_internal_messages (\n'
 '\tinternal_message_id UUID NOT NULL, \n'
 '\tsession_id UUID, \n'
 '\tpatient_id UUID, \n'
 '\tagent_name VARCHAR(64) NOT NULL, \n'
 '\tevent_type VARCHAR(64) NOT NULL, \n'
 '\tcontent TEXT NOT NULL, \n'
 '\tmetadata JSONB NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (internal_message_id), \n'
 '\tFOREIGN KEY(session_id) REFERENCES ai_sessions (session_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE ai_messages (\n'
 '\tmessage_id UUID NOT NULL, \n'
 '\tsession_id UUID NOT NULL, \n'
 '\tsender_role VARCHAR(32) NOT NULL, \n'
 '\tcontent TEXT NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (message_id), \n'
 '\tFOREIGN KEY(session_id) REFERENCES ai_sessions (session_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE care_connection_requests (\n'
 '\trequest_id UUID NOT NULL, \n'
 '\tcare_episode_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tdoctor_id UUID NOT NULL, \n'
 '\trequest_reason TEXT NOT NULL, \n'
 '\tstatus VARCHAR(32) NOT NULL, \n'
 '\tresponded_at TIMESTAMP WITH TIME ZONE, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (request_id), \n'
 '\tCONSTRAINT uq_care_episode_doctor_request UNIQUE (care_episode_id, doctor_id), \n'
 '\tFOREIGN KEY(care_episode_id) REFERENCES care_episodes (care_episode_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(doctor_id) REFERENCES doctors (doctor_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE care_episode_triage_summaries (\n'
 '\ttriage_summary_id UUID NOT NULL, \n'
 '\tcare_episode_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tversion INTEGER NOT NULL, \n'
 '\tconcern VARCHAR(255) NOT NULL, \n'
 '\trelevant_context TEXT NOT NULL, \n'
 '\tsafety_signals JSONB NOT NULL, \n'
 '\tlimitations JSONB NOT NULL, \n'
 '\trecommendation TEXT NOT NULL, \n'
 '\tmissing_information JSONB NOT NULL, \n'
 '\tunresolved_questions JSONB NOT NULL, \n'
 '\tclinician_review_needed BOOLEAN NOT NULL, \n'
 '\tadditional_context TEXT NOT NULL, \n'
 '\tsource_ai_session_id UUID, \n'
 "\tsource_conversation_transcript TEXT DEFAULT '' NOT NULL, \n"
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (triage_summary_id), \n'
 '\tCONSTRAINT uq_care_episode_triage_summary_version UNIQUE (care_episode_id, version), \n'
 '\tFOREIGN KEY(care_episode_id) REFERENCES care_episodes (care_episode_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(source_ai_session_id) REFERENCES ai_sessions (session_id) ON DELETE SET NULL\n'
 ')',
 'CREATE TABLE care_episode_triage_summary_drafts (\n'
 '\ttriage_summary_draft_id UUID NOT NULL, \n'
 '\tcare_episode_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tconcern VARCHAR(255) NOT NULL, \n'
 '\trelevant_context TEXT NOT NULL, \n'
 '\tsafety_signals JSONB NOT NULL, \n'
 '\tlimitations JSONB NOT NULL, \n'
 '\trecommendation TEXT NOT NULL, \n'
 '\tmissing_information JSONB NOT NULL, \n'
 '\tunresolved_questions JSONB NOT NULL, \n'
 '\tclinician_review_needed BOOLEAN NOT NULL, \n'
 '\tadditional_context TEXT NOT NULL, \n'
 '\tsource_ai_session_id UUID, \n'
 "\tsource_conversation_transcript TEXT DEFAULT '' NOT NULL, \n"
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (triage_summary_draft_id), \n'
 '\tFOREIGN KEY(care_episode_id) REFERENCES care_episodes (care_episode_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(source_ai_session_id) REFERENCES ai_sessions (session_id) ON DELETE SET NULL\n'
 ')',
 'CREATE TABLE consultation_messages (\n'
 '\tmessage_id UUID NOT NULL, \n'
 '\tthread_id UUID NOT NULL, \n'
 '\tsender_id UUID NOT NULL, \n'
 '\tcontent TEXT NOT NULL, \n'
 '\tread_receipt BOOLEAN NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (message_id), \n'
 '\tFOREIGN KEY(thread_id) REFERENCES consultation_threads (thread_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE episode_rehab_sessions (\n'
 '\tsession_id UUID NOT NULL, \n'
 '\tcare_episode_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\trecommended_exercises JSONB NOT NULL, \n'
 '\tchecklist JSONB NOT NULL, \n'
 '\tpatient_notes TEXT NOT NULL, \n'
 '\tsession_summary TEXT NOT NULL, \n'
 '\tcompletion_status VARCHAR(32) NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (session_id), \n'
 '\tFOREIGN KEY(care_episode_id) REFERENCES care_episodes (care_episode_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE memory_agent_runs (\n'
 '\trun_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tcare_episode_id UUID, \n'
 '\ttrigger VARCHAR(64) NOT NULL, \n'
 '\tstatus VARCHAR(32) NOT NULL, \n'
 '\tchanges JSONB NOT NULL, \n'
 '\tmetadata JSONB NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (run_id), \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(care_episode_id) REFERENCES care_episodes (care_episode_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE memory_documents (\n'
 '\tdocument_id UUID NOT NULL, \n'
 '\tscope VARCHAR(32) NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tcare_episode_id UUID, \n'
 '\tcompiled_text TEXT NOT NULL, \n'
 '\tstatus VARCHAR(32) NOT NULL, \n'
 '\tlast_compiled_at TIMESTAMP WITH TIME ZONE, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (document_id), \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(care_episode_id) REFERENCES care_episodes (care_episode_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE patient_clinical_memory (\n'
 '\tmemory_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tfact_category VARCHAR(64) NOT NULL, \n'
 '\textracted_fact TEXT NOT NULL, \n'
 '\tsource_session_id UUID, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (memory_id), \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(source_session_id) REFERENCES ai_sessions (session_id) ON DELETE SET NULL\n'
 ')',
 'CREATE TABLE rehab_sessions (\n'
 '\tsession_id UUID NOT NULL, \n'
 '\tplan_id UUID NOT NULL, \n'
 '\tscheduled_time TIMESTAMP WITH TIME ZONE NOT NULL, \n'
 '\tcompletion_status VARCHAR(32) NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (session_id), \n'
 '\tFOREIGN KEY(plan_id) REFERENCES rehab_plans (plan_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE ai_daily_rehab_recommendations (\n'
 '\trecommendation_id UUID NOT NULL, \n'
 '\tcare_episode_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tsource_triage_summary_id UUID, \n'
 '\tsource_triage_summary_version INTEGER NOT NULL, \n'
 '\tstatus VARCHAR(32) NOT NULL, \n'
 '\tempty_reason TEXT NOT NULL, \n'
 '\tquery_text TEXT NOT NULL, \n'
 '\titems JSONB NOT NULL, \n'
 '\tactive BOOLEAN NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (recommendation_id), \n'
 '\tFOREIGN KEY(care_episode_id) REFERENCES care_episodes (care_episode_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(source_triage_summary_id) REFERENCES care_episode_triage_summaries '
 '(triage_summary_id) ON DELETE SET NULL\n'
 ')',
 'CREATE TABLE care_relationships (\n'
 '\trelationship_id UUID NOT NULL, \n'
 '\tcare_episode_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tdoctor_id UUID NOT NULL, \n'
 '\tsource_request_id UUID NOT NULL, \n'
 '\tstatus VARCHAR(32) NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (relationship_id), \n'
 '\tCONSTRAINT uq_care_relationship_episode UNIQUE (care_episode_id), \n'
 '\tFOREIGN KEY(care_episode_id) REFERENCES care_episodes (care_episode_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(doctor_id) REFERENCES doctors (doctor_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(source_request_id) REFERENCES care_connection_requests (request_id) ON DELETE '
 'CASCADE\n'
 ')',
 'CREATE TABLE memory_document_items (\n'
 '\tmemory_item_id UUID NOT NULL, \n'
 '\tdocument_id UUID NOT NULL, \n'
 '\titem_type VARCHAR(64) NOT NULL, \n'
 '\ttitle VARCHAR(255) NOT NULL, \n'
 '\tcontent TEXT NOT NULL, \n'
 '\tstatus VARCHAR(32) NOT NULL, \n'
 '\tvisibility VARCHAR(64) NOT NULL, \n'
 '\tsource_type VARCHAR(64) NOT NULL, \n'
 '\tsource_id TEXT, \n'
 '\tevidence JSONB NOT NULL, \n'
 '\treason TEXT NOT NULL, \n'
 '\tarchived_at TIMESTAMP WITH TIME ZONE, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (memory_item_id), \n'
 '\tFOREIGN KEY(document_id) REFERENCES memory_documents (document_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE ai_care_summaries (\n'
 '\tcare_summary_id UUID NOT NULL, \n'
 '\tcare_episode_id UUID NOT NULL, \n'
 '\trelationship_id UUID NOT NULL, \n'
 '\tconversation_digest TEXT NOT NULL, \n'
 '\tplan_digest TEXT NOT NULL, \n'
 '\tunresolved_questions JSONB NOT NULL, \n'
 '\traw_score_trend JSONB, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (care_summary_id), \n'
 '\tFOREIGN KEY(care_episode_id) REFERENCES care_episodes (care_episode_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(relationship_id) REFERENCES care_relationships (relationship_id) ON DELETE '
 'CASCADE\n'
 ')',
 'CREATE TABLE ai_daily_rehab_lists (\n'
 '\tlist_id UUID NOT NULL, \n'
 '\tcare_episode_id UUID NOT NULL, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tsource_recommendation_id UUID, \n'
 '\titems JSONB NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (list_id), \n'
 '\tCONSTRAINT uq_ai_daily_rehab_list_episode UNIQUE (care_episode_id), \n'
 '\tFOREIGN KEY(care_episode_id) REFERENCES care_episodes (care_episode_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(source_recommendation_id) REFERENCES ai_daily_rehab_recommendations '
 '(recommendation_id) ON DELETE SET NULL\n'
 ')',
 'CREATE TABLE care_appointments (\n'
 '\tappointment_id UUID NOT NULL, \n'
 '\trelationship_id UUID NOT NULL, \n'
 '\tcare_episode_id UUID, \n'
 '\tpatient_id UUID NOT NULL, \n'
 '\tdoctor_id UUID NOT NULL, \n'
 '\tscheduled_start TIMESTAMP WITH TIME ZONE NOT NULL, \n'
 '\tscheduled_end TIMESTAMP WITH TIME ZONE, \n'
 '\tstatus VARCHAR(32) NOT NULL, \n'
 '\tpurpose TEXT NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (appointment_id), \n'
 '\tFOREIGN KEY(relationship_id) REFERENCES care_relationships (relationship_id) ON DELETE '
 'CASCADE, \n'
 '\tFOREIGN KEY(care_episode_id) REFERENCES care_episodes (care_episode_id) ON DELETE SET NULL, \n'
 '\tFOREIGN KEY(patient_id) REFERENCES patients (patient_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(doctor_id) REFERENCES doctors (doctor_id) ON DELETE CASCADE\n'
 ')',
 'CREATE TABLE care_conversation_messages (\n'
 '\tmessage_id UUID NOT NULL, \n'
 '\tcare_episode_id UUID NOT NULL, \n'
 '\trelationship_id UUID NOT NULL, \n'
 '\tsender_id UUID NOT NULL, \n'
 '\tsender_role VARCHAR(32) NOT NULL, \n'
 '\tcontent TEXT NOT NULL, \n'
 '\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n'
 '\tPRIMARY KEY (message_id), \n'
 '\tFOREIGN KEY(care_episode_id) REFERENCES care_episodes (care_episode_id) ON DELETE CASCADE, \n'
 '\tFOREIGN KEY(relationship_id) REFERENCES care_relationships (relationship_id) ON DELETE '
 'CASCADE\n'
 ')',
 'CREATE UNIQUE INDEX ix_rehab_exercise_videos_slug ON rehab_exercise_videos (slug)',
 'CREATE INDEX ix_care_episodes_patient_id ON care_episodes (patient_id)',
 'CREATE INDEX ix_doctor_intelligence_artifacts_artifact_type ON doctor_intelligence_artifacts '
 '(artifact_type)',
 'CREATE INDEX ix_doctor_intelligence_artifacts_doctor_id ON doctor_intelligence_artifacts '
 '(doctor_id)',
 'CREATE INDEX ix_ai_internal_messages_patient_id ON ai_internal_messages (patient_id)',
 'CREATE INDEX ix_ai_internal_messages_session_id ON ai_internal_messages (session_id)',
 'CREATE INDEX ix_care_connection_requests_care_episode_id ON care_connection_requests '
 '(care_episode_id)',
 'CREATE INDEX ix_care_connection_requests_doctor_id ON care_connection_requests (doctor_id)',
 'CREATE INDEX ix_care_connection_requests_patient_id ON care_connection_requests (patient_id)',
 'CREATE INDEX ix_care_episode_triage_summaries_care_episode_id ON care_episode_triage_summaries '
 '(care_episode_id)',
 'CREATE INDEX ix_care_episode_triage_summaries_patient_id ON care_episode_triage_summaries '
 '(patient_id)',
 'CREATE INDEX ix_care_episode_triage_summary_drafts_care_episode_id ON '
 'care_episode_triage_summary_drafts (care_episode_id)',
 'CREATE INDEX ix_care_episode_triage_summary_drafts_patient_id ON '
 'care_episode_triage_summary_drafts (patient_id)',
 'CREATE INDEX ix_episode_rehab_sessions_care_episode_id ON episode_rehab_sessions '
 '(care_episode_id)',
 'CREATE INDEX ix_episode_rehab_sessions_patient_id ON episode_rehab_sessions (patient_id)',
 'CREATE INDEX ix_memory_agent_runs_care_episode_id ON memory_agent_runs (care_episode_id)',
 'CREATE INDEX ix_memory_agent_runs_patient_id ON memory_agent_runs (patient_id)',
 'CREATE INDEX ix_memory_documents_care_episode_id ON memory_documents (care_episode_id)',
 'CREATE INDEX ix_memory_documents_patient_id ON memory_documents (patient_id)',
 'CREATE INDEX ix_memory_documents_scope ON memory_documents (scope)',
 'CREATE UNIQUE INDEX uq_memory_documents_episode_document ON memory_documents (care_episode_id) '
 'WHERE scope = $$episode$$ AND care_episode_id IS NOT NULL',
 'CREATE UNIQUE INDEX uq_memory_documents_patient_document ON memory_documents (patient_id) WHERE '
 'scope = $$patient$$ AND care_episode_id IS NULL',
 'CREATE INDEX ix_ai_daily_rehab_recommendations_care_episode_id ON ai_daily_rehab_recommendations '
 '(care_episode_id)',
 'CREATE INDEX ix_ai_daily_rehab_recommendations_patient_id ON ai_daily_rehab_recommendations '
 '(patient_id)',
 'CREATE INDEX ix_ai_daily_rehab_recommendations_source_triage_summary_id ON '
 'ai_daily_rehab_recommendations (source_triage_summary_id)',
 'CREATE INDEX ix_care_relationships_care_episode_id ON care_relationships (care_episode_id)',
 'CREATE INDEX ix_care_relationships_doctor_id ON care_relationships (doctor_id)',
 'CREATE INDEX ix_care_relationships_patient_id ON care_relationships (patient_id)',
 'CREATE INDEX ix_memory_document_items_document_id ON memory_document_items (document_id)',
 'CREATE INDEX ix_ai_care_summaries_care_episode_id ON ai_care_summaries (care_episode_id)',
 'CREATE INDEX ix_ai_care_summaries_relationship_id ON ai_care_summaries (relationship_id)',
 'CREATE INDEX ix_ai_daily_rehab_lists_care_episode_id ON ai_daily_rehab_lists (care_episode_id)',
 'CREATE INDEX ix_ai_daily_rehab_lists_patient_id ON ai_daily_rehab_lists (patient_id)',
 'CREATE INDEX ix_care_appointments_care_episode_id ON care_appointments (care_episode_id)',
 'CREATE INDEX ix_care_appointments_doctor_id ON care_appointments (doctor_id)',
 'CREATE INDEX ix_care_appointments_patient_id ON care_appointments (patient_id)',
 'CREATE INDEX ix_care_appointments_relationship_id ON care_appointments (relationship_id)',
 'CREATE INDEX ix_care_appointments_scheduled_start ON care_appointments (scheduled_start)',
 'CREATE INDEX ix_care_conversation_messages_care_episode_id ON care_conversation_messages '
 '(care_episode_id)',
 'CREATE INDEX ix_care_conversation_messages_relationship_id ON care_conversation_messages '
 '(relationship_id)')
DOWNGRADE_SQL = ('DROP TABLE care_conversation_messages',
 'DROP TABLE care_appointments',
 'DROP TABLE ai_daily_rehab_lists',
 'DROP TABLE ai_care_summaries',
 'DROP TABLE memory_document_items',
 'DROP TABLE care_relationships',
 'DROP TABLE ai_daily_rehab_recommendations',
 'DROP TABLE rehab_sessions',
 'DROP TABLE patient_clinical_memory',
 'DROP TABLE memory_documents',
 'DROP TABLE memory_agent_runs',
 'DROP TABLE episode_rehab_sessions',
 'DROP TABLE consultation_messages',
 'DROP TABLE care_episode_triage_summary_drafts',
 'DROP TABLE care_episode_triage_summaries',
 'DROP TABLE care_connection_requests',
 'DROP TABLE ai_messages',
 'DROP TABLE ai_internal_messages',
 'DROP TABLE video_pose_extractions',
 'DROP TABLE rehab_plans',
 'DROP TABLE patient_doctor_mappings',
 'DROP TABLE patient_clinical_records',
 'DROP TABLE doctor_intelligence_artifacts',
 'DROP TABLE consultation_threads',
 'DROP TABLE care_episodes',
 'DROP TABLE ai_sessions',
 'DROP TABLE rehab_exercise_videos',
 'DROP TABLE patients',
 'DROP TABLE doctors')


def upgrade() -> None:
    for statement in UPGRADE_SQL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_SQL:
        op.execute(statement)
