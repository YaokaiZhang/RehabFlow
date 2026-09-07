from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class PatientRegisterRequest(BaseModel):
	patient_name: str = Field(min_length=2, max_length=255)
	password: str = Field(min_length=8, max_length=128)
	real_info: dict = Field(default_factory=dict)
	subscription_tier: str = Field(default="self_serve", max_length=64)


class DoctorRegisterRequest(BaseModel):
	doctor_name: str = Field(min_length=2, max_length=255)
	password: str = Field(min_length=8, max_length=128)
	real_info: dict = Field(default_factory=dict)
	verification_status: str = Field(default="pending", max_length=64)


class LoginRequest(BaseModel):
	role: Literal["patient", "doctor"]
	username: str = Field(min_length=2, max_length=255)
	password: str = Field(min_length=8, max_length=128)


class AuthUser(BaseModel):
	user_id: UUID
	role: Literal["patient", "doctor"]
	username: str


class AuthResponse(BaseModel):
	access_token: str
	token_type: str = "bearer"
	user: AuthUser


class BindPatientRequest(BaseModel):
	patient_id: UUID
	status: str = Field(default="active", max_length=32)


class BindingResponse(BaseModel):
	mapping_id: UUID
	patient_id: UUID
	doctor_id: UUID
	status: str
