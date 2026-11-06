#!/usr/bin/env python3
"""Offline contract: the memory agent reads who spoke before what was said."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QWEN_API_KEY", "test-key")
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.ai.prompt_defaults import EPISODE_MEMORY
from app.services.memory_agent import _agent_messages


def test_each_message_lists_role_before_content() -> None:
    snapshot = {
        "trigger": "ai_turn",
        "session_id": "s1",
        "previous_episode_memory": {"level2_summary": "Patient answered yes to leg pain."},
        "current_session_ai_messages": [
            {"role": "assistant", "content": "Does the pain spread into your buttock or leg?", "sequence": 1},
            {"role": "user", "content": "no", "sequence": 2},
        ],
    }
    prompt = str(_agent_messages("episode", snapshot, "")[1].content)
    question = prompt.index('"role": "assistant", "content": "Does the pain spread')
    answer = prompt.index('"role": "user", "content": "no"')
    assert question < answer


def test_episode_prompt_attributes_answers_and_lets_messages_correct_memory() -> None:
    assert "role user" in EPISODE_MEMORY
    assert "stop-instruction" in EPISODE_MEMORY
    assert "messages are the source of truth" in EPISODE_MEMORY


def main() -> None:
    test_each_message_lists_role_before_content()
    test_episode_prompt_attributes_answers_and_lets_messages_correct_memory()
    print("memory agent message order contract ok")


if __name__ == "__main__":
    main()
