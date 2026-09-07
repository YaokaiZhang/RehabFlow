from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import triage_summary_agent


class _Settings:
    qwen_api_key = None
    qwen_api_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class _Episode:
    care_episode_id = "00000000-0000-0000-0000-000000000001"
    patient_id = "00000000-0000-0000-0000-000000000002"
    issue_title = "Left knee stiffness"
    body_area = "Left Knee"
    goal = "Relieve knee stiffness"
    short_description = "Cold-induced stiffness after an old fracture."


def main() -> None:
    original = os.environ.pop("REHAB_TRIAGE_SUMMARY_AGENT", None)
    try:
        assert triage_summary_agent._ai_summary_enabled() is True
        os.environ["REHAB_TRIAGE_SUMMARY_AGENT"] = "fallback"
        assert triage_summary_agent._ai_summary_enabled() is False
        os.environ["REHAB_TRIAGE_SUMMARY_AGENT"] = "qwen"
        assert triage_summary_agent._ai_summary_enabled() is True

        os.environ["REHAB_TRIAGE_SUMMARY_AGENT"] = "qwen"
        original_key = os.environ.pop("QWEN_API_KEY", None)
        original_get_settings = triage_summary_agent.get_settings
        triage_summary_agent.get_settings = lambda: _Settings()
        try:
            try:
                triage_summary_agent._try_build_ai_summary(_Episode(), "", "Patient: knee stiffness", 0)
            except Exception as exc:
                assert exc.__class__.__name__ == "TriageSummaryGenerationFailed"
            else:
                raise AssertionError("LLM mode must not silently fall back when the API key is missing")
        finally:
            triage_summary_agent.get_settings = original_get_settings
            if original_key is not None:
                os.environ["QWEN_API_KEY"] = original_key
    finally:
        if original is None:
            os.environ.pop("REHAB_TRIAGE_SUMMARY_AGENT", None)
        else:
            os.environ["REHAB_TRIAGE_SUMMARY_AGENT"] = original
    print("triage summary llm mode contract ok")


if __name__ == "__main__":
    main()
