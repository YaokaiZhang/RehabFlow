from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import get_current_principal
from app.db.models import Patient
from app.db.session import get_db
from app.schemas.exercise_catalog import ExerciseCatalogFacetsResponse, ExerciseCatalogItem, ExerciseCatalogSearchResponse
from app.services.exercise_catalog import get_exercise, get_facets, search_exercises

router = APIRouter(prefix="/exercise-catalog", tags=["exercise-catalog"])


def _require_patient(principal: dict, db: Session) -> UUID:
	if principal.get("role") != "patient":
		raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only patients can use Exercise Catalog")
	try:
		patient_id = UUID(str(principal.get("user_id")))
	except ValueError as exc:
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing patient identity") from exc
	if db.get(Patient, patient_id) is None:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
	return patient_id


@router.get("/facets", response_model=ExerciseCatalogFacetsResponse)
def list_exercise_catalog_facets(
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> dict[str, list[str]]:
	_require_patient(principal, db)
	return get_facets()


@router.get("", response_model=ExerciseCatalogSearchResponse)
def list_exercise_catalog(
	query: str | None = Query(default=None, max_length=200),
	structure: str | None = Query(default=None, max_length=200),
	condition: str | None = Query(default=None, max_length=200),
	limit: int = Query(default=20, ge=1, le=50),
	offset: int = Query(default=0, ge=0),
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> dict:
	_require_patient(principal, db)
	return search_exercises(query=query, structure=structure, condition=condition, limit=limit, offset=offset)


@router.get("/{exercise_id}", response_model=ExerciseCatalogItem)
def get_exercise_catalog_item(
	exercise_id: str,
	principal: dict = Depends(get_current_principal),
	db: Session = Depends(get_db),
) -> dict:
	_require_patient(principal, db)
	exercise = get_exercise(exercise_id)
	if exercise is None:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exercise not found")
	return exercise.to_response()
