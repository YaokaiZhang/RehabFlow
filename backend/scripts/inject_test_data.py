#!/usr/bin/env python3
"""
Script to inject test data into the database for testing purposes.
"""

import os
import sys
import uuid

# Ensure backend package is importable when running this script directly.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SCRIPT_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.db.models import Patient
from app.db.session import SessionLocal

def inject_test_data():
    db = SessionLocal()
    try:
        # Check if test patient already exists
        test_patient_id = uuid.UUID("f1a04b85-b4a1-4ac4-9378-58940ff6ddf8")
        existing_patient = db.query(Patient).filter(Patient.patient_id == test_patient_id).first()
        
        if existing_patient:
            print(f"Test patient already exists: {test_patient_id}")
            return test_patient_id
        
        # Create test patient
        test_patient = Patient(
            patient_id=test_patient_id,
            patient_name="Test Patient_0408",
            password_hash="dummy_hash",
            real_info={"test": True, "created_for": "ai_chat_testing"}
        )
        db.add(test_patient)
        db.commit()
        print(f"Injected test patient: {test_patient_id}")
        return test_patient_id
    except Exception as e:
        db.rollback()
        print(f"Error injecting test data: {e}")
        return None
    finally:
        db.close()

if __name__ == "__main__":
    inject_test_data()