#!/usr/bin/env python3
"""
AI Chat Test Script

This script allows you to test the AI chat functionality through a terminal interface.
It connects to the WebSocket endpoint and enables real-time multi-turn conversations.

Usage:
    python test_ai_chat.py [session_id]

If session_id is not provided, a new session will be created.
"""

import asyncio
import json
import sys
import uuid
from typing import Optional
import traceback

import websockets


async def chat_client(session_id: Optional[str] = None):
    uri = f"ws://localhost:8001/ai/chat/{session_id or 'new'}"
    # Test patient ID - make sure this exists in the database
    test_patient_id = "f1a04b85-b4a1-4ac4-9378-58940ff6ddf8"

    try:
        async with websockets.connect(uri, ping_interval=None) as websocket:
            print("Connected to AI chat. Type your messages (type 'quit' to exit):")
            print("-" * 50)

            while True:
                # Get user input
                user_input = input("You: ").strip()
                if user_input.lower() in ['quit', 'exit', 'q']:
                    break

                if not user_input:
                    continue

                # Send message with patient_id
                message = {"message": user_input, "patient_id": test_patient_id}
                await websocket.send(json.dumps(message))

                # Receive response
                response = await websocket.recv()
                data = json.loads(response)

                if data.get("event") == "response":
                    print(f"AI: {data['response']}")
                    if session_id is None:
                        session_id = data['session_id']
                        print(f"New session created: {session_id}")
                elif data.get("event") == "error":
                    print(f"Error: {data['detail']}")
                else:
                    print(f"Unknown event: {data}")

                print("-" * 50)

    except websockets.exceptions.ConnectionClosed as e:
        print(f"Connection closed by server. Code: {e.code}, Reason: {e.reason}")
    except Exception as e:
        print(f"Error Type: {type(e).__name__}")
        print(f"Error Details: {e}")
        print("Full Traceback:")
        traceback.print_exc()


def main():
    if len(sys.argv) > 1:
        session_id = sys.argv[1]
        try:
            uuid.UUID(session_id)  # Validate UUID format
        except ValueError:
            print("Invalid session ID format. Must be a valid UUID.")
            sys.exit(1)
    else:
        session_id = None

    print("Starting AI Chat Test Client...")
    print(f"Session ID: {session_id or 'New session will be created'}")
    print("Make sure the FastAPI server is running on http://localhost:8001")

    asyncio.run(chat_client(session_id))


if __name__ == "__main__":
    main()