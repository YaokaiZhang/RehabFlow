# Combined v2 diagnostic artifact

- Status: failed
- Failure phase: backend_startup
- Failure type: RuntimeError
- Failure message: [REDACTED]
- No complete 8 + 26 case report was available.
- Cleanup status: failed
- Cleanup failures: qdrant_RuntimeError

JSON payload:

{
  "cleanup": {
    "failures": [
      "qdrant_RuntimeError"
    ],
    "status": "failed"
  },
  "counts": {
    "longitudinal_scenarios": 0,
    "real_model_cases": 0,
    "total": 0
  },
  "diagnostic": {
    "error_message": "[REDACTED]",
    "error_type": "RuntimeError",
    "phase": "backend_startup"
  },
  "generated_at": "2026-08-01T15:16:06.368593+00:00",
  "report_version": "combined-v2-diagnostic",
  "run_metadata": {
    "backend_revision": "test-backend-revision",
    "bootstrap_cleanup": null,
    "cleanup": {
      "failures": [
        "qdrant_RuntimeError"
      ],
      "status": "failed"
    },
    "evaluator_dirty_state": "not-applicable",
    "evaluator_revision": "unversioned",
    "execution_mode": "real-backend-service",
    "manifest": {
      "case_count": 1,
      "cases": [
        {
          "case_hash": "5a28b05534f3e2cdb7ad",
          "resource_counts": {
            "ai_sessions": 0,
            "care_episodes": 0,
            "catalog_files": 1,
            "checkpoint_threads": 0,
            "memory_documents": 0,
            "memory_items": 0,
            "patients": 1,
            "qdrant_collections": 1,
            "qdrant_paths": 0,
            "triage_summaries": 0
          },
          "resource_hashes": {
            "care_episodes": [],
            "catalog_path": "6842c215899cbf6bec77",
            "checkpoint_threads": [],
            "memory_items": [],
            "patient": "0ac84dca65a647914cb9",
            "qdrant_collection": "3ced9b8fbf200182057b",
            "qdrant_path": null,
            "sessions": [],
            "triage_summaries": []
          }
        }
      ],
      "database_bootstrap": null,
      "qdrant_endpoint_binding": null,
      "resource_counts": {
        "ai_sessions": 0,
        "care_episodes": 0,
        "catalog_files": 1,
        "checkpoint_threads": 0,
        "memory_documents": 0,
        "memory_items": 0,
        "patients": 1,
        "qdrant_collections": 1,
        "qdrant_paths": 0,
        "triage_summaries": 0
      },
      "run_hash": "2c957c4f9f0ea6152739"
    }
  },
  "status": "failed"
}
