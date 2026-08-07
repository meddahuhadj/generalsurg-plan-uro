# -*- coding: utf-8 -*-
"""
tests/test_surgical_plans.py — CRUD + cycle de vie des plans chirurgicaux persistés
======================================================================================
Avant ce correctif, le « plan » était un export JSON local côté frontend, jamais
persisté : rien ne le retraçait (pas d'audit), rien ne validait le cycle de vie
clinique (DRAFT → APPROVED → IN_PROGRESS → COMPLETED, ABORTED à tout moment).

Teste les endpoints routers/plans.py contre une vraie base SQLite en mémoire
(remplacement de la dépendance get_db + get_current_user court-circuité) :
    - CRUD complet
    - 401 sans authentification
    - 404 patient inconnu
    - 404 plan inconnu
    - transitions de statut autorisées / refusées (409)
    - plan COMPLETED/ABORTED figé
    - audibilité : chaque opération écrit une entrée d'audit
    - validation clinique explicite (POST /approve, /abort) : PUT générique bloqué sur
      APPROVED/ABORTED, rôle vérifié pour l'approbation, confirmation/motif obligatoires,
      identité + horodatage de l'approbateur/annulateur persistés sur le plan

Lancer : cd backend && pytest tests/test_surgical_plans.py -v
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

import models
import main
from db import get_db
from deps import get_current_user


@pytest.fixture()
def client():
    """Base SQLite en mémoire isolée + TestClient avec auth court-circuitée."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def _override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    fake_user = models.User(id="u1", username="dr.plan", full_name="Dr. Plan",
                            role="surgeon", hashed_password="x", is_active=True)
    main.app.dependency_overrides[get_db] = _override_get_db
    main.app.dependency_overrides[get_current_user] = lambda: fake_user

    client = TestClient(main.app)
    db = SessionLocal()
    db.add(models.Patient(id="P-PLAN-1", nom="Test Plan", age=58, sexe="M", poids_kg=78.0,
                          taille_cm=172.0, diagnostic="Tumeur hépatique", chirurgien="Dr X",
                          specialty="hbp"))
    db.commit()
    db.close()
    yield client
    main.app.dependency_overrides.clear()


def _create_plan(client, **overrides):
    payload = {
        "title": "Hépatectomie droite élargie",
        "specialty": "hbp",
        "planned_procedure_code": "CCAM-HMFA004",
        "strategy_status": "DRAFT",
        "safety_margins_mm": 5.0,
        "resection_volume_ml": 640.0,
        "remnant_volume_ml": 760.0,
        "remnant_ratio_pct": 54.3,
        "metadata_json": {"organ_volume_ml": 1400.0, "lesion_volume_ml": 35.0,
                          "volume_source": "real_segmentation"},
    }
    payload.update(overrides)
    return client.post("/api/v1/patients/P-PLAN-1/plans", json=payload)


def test_requires_authentication(client):
    main.app.dependency_overrides.pop(get_current_user, None)
    try:
        r = client.get("/api/v1/patients/P-PLAN-1/plans")
        assert r.status_code == 401
    finally:
        main.app.dependency_overrides[get_current_user] = lambda: models.User(
            id="u1", username="dr.plan", full_name="Dr. Plan", role="surgeon",
            hashed_password="x", is_active=True)


def test_create_and_get_plan(client):
    r = _create_plan(client)
    assert r.status_code == 201
    body = r.json()
    assert body["id"]
    assert body["lead_surgeon_username"] == "dr.plan"
    assert body["strategy_status"] == "DRAFT"
    assert body["resection_volume_ml"] == 640.0
    assert body["metadata_json"]["volume_source"] == "real_segmentation"
    assert body["twin_id"] is None  # aucun jumeau numérique persisté

    g = client.get(f"/api/v1/plans/{body['id']}")
    assert g.status_code == 200
    assert g.json()["title"] == "Hépatectomie droite élargie"


def test_list_plans_per_patient(client):
    _create_plan(client, title="Plan A")
    _create_plan(client, title="Plan B")
    r = client.get("/api/v1/patients/P-PLAN-1/plans")
    assert r.status_code == 200
    titles = {p["title"] for p in r.json()}
    assert titles == {"Plan A", "Plan B"}


def test_plan_persists_volume_source_flag(client):
    """Le statut du plan reste honnête : la source des volumes est retracée
    (segmentation réelle vs estimation de population)."""
    r = _create_plan(client, metadata_json={"volume_source": "population_estimate"})
    assert r.status_code == 201
    plan_id = r.json()["id"]
    g = client.get(f"/api/v1/plans/{plan_id}").json()
    assert g["metadata_json"]["volume_source"] == "population_estimate"


def test_create_plan_unknown_patient_404(client):
    r = client.post("/api/v1/patients/INCONNU/plans",
                    json={"title": "Plan", "specialty": "hbp"})
    assert r.status_code == 404


def test_get_unknown_plan_404(client):
    assert client.get("/api/v1/plans/inexistant").status_code == 404


def test_update_plan_title(client):
    plan_id = _create_plan(client).json()["id"]
    r = client.put(f"/api/v1/plans/{plan_id}", json={"title": "Hépatectomie gauche"})
    assert r.status_code == 200
    assert r.json()["title"] == "Hépatectomie gauche"


def _approve(client, plan_id, confirmation=True, comment=None):
    body = {"confirmation": confirmation}
    if comment is not None:
        body["comment"] = comment
    return client.post(f"/api/v1/plans/{plan_id}/approve", json=body)


def _abort(client, plan_id, reason="Contre-indication découverte en RCP"):
    return client.post(f"/api/v1/plans/{plan_id}/abort", json={"reason": reason})


def test_status_lifecycle_valid_transitions(client):
    plan_id = _create_plan(client).json()["id"]

    ok = _approve(client, plan_id)
    assert ok.status_code == 200
    assert ok.json()["strategy_status"] == "APPROVED"
    assert ok.json()["approved_by_username"] == "dr.plan"
    assert ok.json()["approved_at"] is not None

    ok = client.put(f"/api/v1/plans/{plan_id}", json={"strategy_status": "IN_PROGRESS"})
    assert ok.status_code == 200
    assert ok.json()["strategy_status"] == "IN_PROGRESS"

    ok = client.put(f"/api/v1/plans/{plan_id}", json={"strategy_status": "COMPLETED"})
    assert ok.status_code == 200
    assert ok.json()["strategy_status"] == "COMPLETED"


def test_status_transition_skipping_invalid_409(client):
    """DRAFT -> COMPLETED directement est refusé (on ne peut pas sauter d'étapes)."""
    plan_id = _create_plan(client).json()["id"]
    r = client.put(f"/api/v1/plans/{plan_id}", json={"strategy_status": "COMPLETED"})
    assert r.status_code == 409
    assert "Transition de statut refusée" in r.json()["detail"]


def test_put_direct_to_approved_rejected(client):
    """APPROVED ne peut plus être atteint par un PUT générique — doit passer par /approve
    (rôle vérifié, confirmation explicite, identité tracée)."""
    plan_id = _create_plan(client).json()["id"]
    r = client.put(f"/api/v1/plans/{plan_id}", json={"strategy_status": "APPROVED"})
    assert r.status_code == 409
    assert "/approve" in r.json()["detail"]
    assert client.get(f"/api/v1/plans/{plan_id}").json()["strategy_status"] == "DRAFT"


def test_put_direct_to_aborted_rejected(client):
    """ABORTED ne peut plus être atteint par un PUT générique — doit passer par /abort
    (motif obligatoire tracé)."""
    plan_id = _create_plan(client).json()["id"]
    r = client.put(f"/api/v1/plans/{plan_id}", json={"strategy_status": "ABORTED"})
    assert r.status_code == 409
    assert "/abort" in r.json()["detail"]


def test_approve_requires_confirmation_true(client):
    plan_id = _create_plan(client).json()["id"]
    r = _approve(client, plan_id, confirmation=False)
    assert r.status_code == 422
    assert client.get(f"/api/v1/plans/{plan_id}").json()["strategy_status"] == "DRAFT"


def test_approve_requires_surgeon_or_admin_role(client):
    main.app.dependency_overrides[get_current_user] = lambda: models.User(
        id="u2", username="secretariat", full_name="Secrétariat", role="viewer",
        hashed_password="x", is_active=True)
    plan_id = _create_plan(client).json()["id"]
    r = _approve(client, plan_id)
    assert r.status_code == 403
    assert client.get(f"/api/v1/plans/{plan_id}").json()["strategy_status"] == "DRAFT"


def test_approve_twice_rejected_409(client):
    plan_id = _create_plan(client).json()["id"]
    assert _approve(client, plan_id).status_code == 200
    r = _approve(client, plan_id)
    assert r.status_code == 409


def test_abort_requires_reason(client):
    plan_id = _create_plan(client).json()["id"]
    r = client.post(f"/api/v1/plans/{plan_id}/abort", json={"reason": "x"[:0]})
    assert r.status_code == 422  # min_length=3, chaîne vide refusée
    r = client.post(f"/api/v1/plans/{plan_id}/abort", json={})
    assert r.status_code == 422  # champ requis


def test_abort_records_reason_and_identity(client):
    plan_id = _create_plan(client).json()["id"]
    r = _abort(client, plan_id, reason="Découverte d'une contre-indication anesthésique")
    assert r.status_code == 200
    body = r.json()
    assert body["strategy_status"] == "ABORTED"
    assert body["aborted_by_username"] == "dr.plan"
    assert body["aborted_at"] is not None
    assert body["abort_reason"] == "Découverte d'une contre-indication anesthésique"


def test_status_abort_always_allowed_then_frozen(client):
    plan_id = _create_plan(client).json()["id"]
    ok = _abort(client, plan_id)
    assert ok.status_code == 200
    assert ok.json()["strategy_status"] == "ABORTED"
    # figé : plus aucun changement autorisé
    r = _approve(client, plan_id)
    assert r.status_code == 409
    r = client.put(f"/api/v1/plans/{plan_id}", json={"title": "Modifier quand même"})
    assert r.status_code == 409


def test_completed_plan_frozen(client):
    plan_id = _create_plan(client).json()["id"]
    _approve(client, plan_id)
    client.put(f"/api/v1/plans/{plan_id}", json={"strategy_status": "IN_PROGRESS"})
    client.put(f"/api/v1/plans/{plan_id}", json={"strategy_status": "COMPLETED"})
    r = client.put(f"/api/v1/plans/{plan_id}", json={"resection_volume_ml": 1.0})
    assert r.status_code == 409


def test_delete_plan(client):
    plan_id = _create_plan(client).json()["id"]
    r = client.delete(f"/api/v1/plans/{plan_id}")
    assert r.status_code == 200
    assert client.get(f"/api/v1/plans/{plan_id}").status_code == 404


def test_operations_are_audited(client):
    main.app.dependency_overrides[get_current_user] = lambda: models.User(
        id="u1", username="dr.plan", full_name="Dr. Plan", role="admin",
        hashed_password="x", is_active=True)
    plan_id = _create_plan(client).json()["id"]
    _approve(client, plan_id)
    r = client.get("/api/v1/audit")
    assert r.status_code == 200
    actions = [e["action"] for e in r.json()]
    assert any("Création plan chirurgical" in a for a in actions)
    assert any("Validation clinique du plan" in a for a in actions)


def test_plans_cascade_delete_with_patient(client):
    plan_id = _create_plan(client).json()["id"]
    r = client.delete("/api/v1/patients/P-PLAN-1")
    assert r.status_code == 200
    assert client.get(f"/api/v1/plans/{plan_id}").status_code == 404
