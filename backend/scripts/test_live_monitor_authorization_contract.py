from __future__ import annotations

import asyncio
import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.api.ws as ws_module
from app.api.ws import _is_doctor_authorized_for_episode_monitor, doctor_monitor_stream
from app.db.session import init_db
from app.main import app

UNAUTHORIZED_MONITOR_DETAIL = "Unauthorized Care Episode monitor access."


def _auth(token: str) -> dict[str, str]:
	return {"Authorization": f"Bearer {token}"}


def _register_patient(client: TestClient, name: str) -> str:
	response = client.post(
		"/auth/register/patient",
		json={"patient_name": name, "password": "live-monitor-pass-123", "real_info": {}},
	)
	assert response.status_code == 200, response.text
	return response.json()["access_token"]


def _register_doctor(client: TestClient, name: str) -> tuple[str, str]:
	response = client.post(
		"/auth/register/doctor",
		json={"doctor_name": name, "password": "live-monitor-pass-123", "real_info": {"specialty": "PT"}},
	)
	assert response.status_code == 200, response.text
	data = response.json()
	return data["access_token"], data["user"]["user_id"]


def _create_episode(client: TestClient, token: str) -> dict:
	response = client.post(
		"/care-episodes",
		json={
			"issue_title": "Live monitor readiness",
			"body_area": "Shoulder",
			"goal": "Practice with optional live movement monitoring",
			"short_description": "Patient wants professional support during home exercise.",
		},
		headers=_auth(token),
	)
	assert response.status_code == 201, response.text
	return response.json()


def _authorized(doctor_id: str, care_episode_id: str) -> bool:
	return asyncio.run(_is_doctor_authorized_for_episode_monitor(doctor_id, care_episode_id))


def _assert_doctor_monitor_ignores_query_param_doctor_id() -> None:
	source = inspect.getsource(doctor_monitor_stream)
	assert "query_params.get(\"doctor_id\"" not in source
	assert "query_params.get(doctor_id" not in source


def _assert_doctor_monitor_route_shape() -> None:
	paths = {getattr(route, "path", "") for route in app.routes}
	assert "/doctor/monitor/episodes/{care_episode_id}" in paths
	assert "/doctor/monitor/{patient_id}" not in paths


def _assert_unauthorized_monitor_payload(client: TestClient, doctor_token: str, episode_id: str) -> None:
	with client.websocket_connect(f"/doctor/monitor/episodes/{episode_id}?access_token={doctor_token}") as websocket:
		message = websocket.receive_json()
		assert message == {"event": "error", "detail": UNAUTHORIZED_MONITOR_DETAIL}
		try:
			websocket.receive_json()
		except WebSocketDisconnect as exc:
			assert exc.code == 1008
		else:
			raise AssertionError("Expected doctor monitor websocket to close with policy violation")


class _FakePubSub:
	async def subscribe(self, channel: str) -> None:
		self.channel = channel

	async def get_message(self, **kwargs):
		await asyncio.sleep(0.01)
		return None

	async def unsubscribe(self, channel: str) -> None:
		self.unsubscribed_channel = channel

	async def close(self) -> None:
		self.closed = True


class _FakeRedis:
	def pubsub(self) -> _FakePubSub:
		return _FakePubSub()

	async def close(self) -> None:
		self.closed = True


def _assert_authorized_monitor_subscription(client: TestClient, doctor_token: str, episode_id: str, patient_id: str) -> None:
	original_from_url = ws_module.Redis.from_url
	ws_module.Redis.from_url = lambda *args, **kwargs: _FakeRedis()
	try:
		with client.websocket_connect(f"/doctor/monitor/episodes/{episode_id}?access_token={doctor_token}") as websocket:
			message = websocket.receive_json()
			assert message == {"event": "subscribed", "care_episode_id": episode_id}
			assert "channel" not in message
			assert str(patient_id) not in str(message)
	finally:
		ws_module.Redis.from_url = original_from_url


def main() -> None:
	_assert_doctor_monitor_ignores_query_param_doctor_id()
	_assert_doctor_monitor_route_shape()
	init_db()
	client = TestClient(app)
	suffix = str(time.time_ns())
	patient_token = _register_patient(client, f"lm_patient_{suffix}")
	doctor_one_token, doctor_one_id = _register_doctor(client, f"lm_doctor_one_{suffix}")
	_, doctor_two_id = _register_doctor(client, f"lm_doctor_two_{suffix}")
	episode = _create_episode(client, patient_token)
	episode_id = episode["care_episode_id"]
	patient_id = episode["patient_id"]

	_assert_unauthorized_monitor_payload(client, doctor_one_token, episode_id)

	subscribe_response = client.post("/professional-care/subscribe", headers=_auth(patient_token))
	assert subscribe_response.status_code == 200, subscribe_response.text

	request_one = client.post(
		f"/care-episodes/{episode_id}/connection-requests",
		json={"doctor_id": doctor_one_id, "request_reason": "Please monitor my form live."},
		headers=_auth(patient_token),
	)
	assert request_one.status_code == 201, request_one.text
	request_one_data = request_one.json()
	request_one_id = request_one_data["request_id"]

	assert _authorized(doctor_one_id, episode_id) is False

	accept_response = client.post(f"/doctor/care-requests/{request_one_id}/accept", headers=_auth(doctor_one_token))
	assert accept_response.status_code == 200, accept_response.text

	assert _authorized(doctor_one_id, episode_id) is False

	select_response = client.post(
		f"/care-episodes/{episode_id}/care-relationship/select",
		json={"doctor_id": doctor_one_id},
		headers=_auth(patient_token),
	)
	assert select_response.status_code == 200, select_response.text

	assert _authorized(doctor_one_id, episode_id) is True
	_assert_authorized_monitor_subscription(client, doctor_one_token, episode_id, patient_id)
	selected_doctor_detail = client.get(f"/care-episodes/{episode_id}", headers=_auth(doctor_one_token))
	assert selected_doctor_detail.status_code == 200, selected_doctor_detail.text
	selected_doctor_episode = selected_doctor_detail.json()
	assert selected_doctor_episode["care_episode_id"] == episode_id
	assert selected_doctor_episode["issue_title"] == "Live monitor readiness"

	unselected_doctor_token, doctor_three_id = _register_doctor(client, f"lm_doctor_three_{suffix}")
	unselected_doctor_detail = client.get(f"/care-episodes/{episode_id}", headers=_auth(unselected_doctor_token))
	assert unselected_doctor_detail.status_code in {403, 404}, unselected_doctor_detail.text
	assert _authorized(doctor_two_id, episode_id) is False
	assert _authorized(doctor_three_id, episode_id) is False
	assert _authorized(doctor_one_id, "not-a-uuid") is False

	print("live monitor authorization contract ok")


if __name__ == "__main__":
	main()
