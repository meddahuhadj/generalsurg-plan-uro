# -*- coding: utf-8 -*-
"""
tests/test_segmentation_db_persistence.py — Persistance des volumes IA en base
================================================================================
Avant ce correctif, les résultats d'un job de segmentation IA réel (volumes en
mL, voir segmentation_service._run_segmentation_job) restaient coincés dans le
job en mémoire (_JOBS) : jamais écrits comme `models.Segment`, donc
GET /patients/{id}/volumetrie (néphrométrie RENAL, FLR/TLV) retombait TOUJOURS
sur une constante de population, même après une segmentation réussie.

Teste `segmentation_service._persist_segments_to_db` directement contre une
vraie base SQLite isolée (pas la base de l'application, pas de mock du DB
engine) — même philosophie que le reste de la suite : préférer une vraie
écriture/lecture à un mock quand c'est possible.

Lancer : cd backend && pytest tests/test_segmentation_db_persistence.py -v
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import segmentation_service as seg


@pytest.fixture()
def db_session_factory(monkeypatch):
    """Base SQLite en mémoire, isolée du fichier generalsurg.db de l'appli.
    StaticPool : une seule connexion partagée, nécessaire pour qu'une base
    SQLite ':memory:' survive entre les sessions ouvertes/fermées par
    _persist_segments_to_db et par le test lui-même."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(seg, "SessionLocal", TestSessionLocal)
    return TestSessionLocal


def _make_patient(session_factory, patient_id="P-TEST-1", specialty="urologie"):
    db = session_factory()
    db.add(models.Patient(id=patient_id, nom="Test Patient", age=55, sexe="M", poids_kg=80.0,
                           taille_cm=175.0, diagnostic="test", chirurgien="Dr Test", specialty=specialty))
    db.commit()
    db.close()


def _segments_for(patient_id, session_factory):
    db = session_factory()
    rows = db.query(models.Segment).filter(models.Segment.patient_id == patient_id).all()
    db.close()
    return rows


def test_persist_creates_organe_segments_for_urologie_kidneys(db_session_factory):
    _make_patient(db_session_factory)
    result_segments = [
        {"organ": "kidney_left", "type": "organe", "volume_ml": 140.0, "label": "Rein gauche", "mesh_url": "/meshes/j1/kidney_left.glb"},
        {"organ": "kidney_right", "type": "organe", "volume_ml": 150.0, "label": "Rein droit"},
    ]
    seg._persist_segments_to_db("P-TEST-1", "job1", result_segments)

    rows = _segments_for("P-TEST-1", db_session_factory)
    assert len(rows) == 2
    by_label = {r.label: r for r in rows}
    assert by_label["Rein gauche"].volume_ml == 140.0
    assert by_label["Rein gauche"].type == "organe"
    assert by_label["Rein gauche"].mesh_ref == "/meshes/j1/kidney_left.glb"
    assert all((r.metadata_json or {}).get("source") == "ai_segmentation" for r in rows)
    assert all((r.metadata_json or {}).get("job_id") == "job1" for r in rows)


def test_persist_maps_foie_to_organe_and_tumeur_to_lesion(db_session_factory):
    _make_patient(db_session_factory, specialty="hbp")
    result_segments = [
        {"organ": "liver", "type": "foie", "volume_ml": 1400.0, "label": "Foie total"},
        {"organ": "liver_tumor", "type": "tumeur", "volume_ml": 35.0, "label": "Tumeur hépatique"},
    ]
    seg._persist_segments_to_db("P-TEST-1", "job2", result_segments)

    rows = {r.label: r for r in _segments_for("P-TEST-1", db_session_factory)}
    assert rows["Foie total"].type == "organe"
    assert rows["Tumeur hépatique"].type == "lesion"


def test_persist_kidney_cyst_lesion_entries_from_urologie_pipeline(db_session_factory):
    """Les kystes rénaux (segmentation_service._run_urologie_cyst_segmentation) arrivent
    déjà avec type="lesion" (passthrough dans _SEGMENT_TYPE_TO_DB_TYPE) — pas de mapping
    intermédiaire comme "tumeur" côté hépatique."""
    _make_patient(db_session_factory, specialty="urologie")
    result_segments = [
        {"organ": "kidney_left", "type": "organe", "volume_ml": 140.0, "label": "Rein gauche"},
        {"organ": "kidney_cyst_left", "type": "lesion", "volume_ml": 4.5, "label": "Kyste rénal gauche"},
    ]
    seg._persist_segments_to_db("P-TEST-1", "job-cyst", result_segments)

    rows = {r.label: r for r in _segments_for("P-TEST-1", db_session_factory)}
    assert rows["Rein gauche"].type == "organe"
    assert rows["Kyste rénal gauche"].type == "lesion"
    assert rows["Kyste rénal gauche"].volume_ml == 4.5


def test_persist_skips_couinaud_sub_segments_to_avoid_double_counting(db_session_factory):
    """Les 8 segments de Couinaud sont des PARTIES du foie déjà comptées dans
    l'entrée "foie" — les persister aussi en type="organe" ferait sommer le
    volume hépatique en double dans /patients/{id}/volumetrie."""
    _make_patient(db_session_factory, specialty="hbp")
    result_segments = [
        {"organ": "liver_segment", "type": "segment", "segment_id": "I", "volume_ml": 120.0, "label": "liver_segment_1"},
        {"organ": "liver", "type": "foie", "volume_ml": 1400.0, "label": "Foie total"},
    ]
    seg._persist_segments_to_db("P-TEST-1", "job3", result_segments)

    rows = _segments_for("P-TEST-1", db_session_factory)
    assert len(rows) == 1
    assert rows[0].label == "Foie total"


def test_persist_skips_zero_volume_entries(db_session_factory):
    """Pas de tumeur détectée (vess_volumes.get("liver_tumor", 0.0) == 0.0) :
    ne doit pas créer un segment "lesion" fantôme à volume nul."""
    _make_patient(db_session_factory, specialty="hbp")
    result_segments = [{"organ": "liver_tumor", "type": "tumeur", "volume_ml": 0.0}]
    seg._persist_segments_to_db("P-TEST-1", "job4", result_segments)
    assert _segments_for("P-TEST-1", db_session_factory) == []


def test_persist_replaces_prior_ai_run_but_keeps_manual_segments(db_session_factory):
    """Une deuxième segmentation ne doit ni accumuler de doublons IA, ni toucher
    aux segments saisis manuellement par un clinicien (POST /patients/{id}/segments,
    sans metadata.source="ai_segmentation")."""
    _make_patient(db_session_factory)
    db = db_session_factory()
    db.add(models.Segment(id="manual-1", patient_id="P-TEST-1", type="organe",
                           volume_ml=99.0, label="Saisie manuelle", metadata_json={}))
    db.commit()
    db.close()

    seg._persist_segments_to_db("P-TEST-1", "job-a", [
        {"organ": "kidney_left", "type": "organe", "volume_ml": 140.0, "label": "Rein gauche"},
    ])
    seg._persist_segments_to_db("P-TEST-1", "job-b", [
        {"organ": "kidney_left", "type": "organe", "volume_ml": 145.0, "label": "Rein gauche"},
    ])

    rows = _segments_for("P-TEST-1", db_session_factory)
    ai_rows = [r for r in rows if (r.metadata_json or {}).get("source") == "ai_segmentation"]
    manual_rows = [r for r in rows if r.id == "manual-1"]
    assert len(ai_rows) == 1  # la 2e run a remplacé la 1re, pas accumulé de doublon
    assert ai_rows[0].volume_ml == 145.0
    assert len(manual_rows) == 1
    assert manual_rows[0].volume_ml == 99.0  # jamais touché


def test_persist_no_ops_silently_when_patient_missing(db_session_factory):
    """Le patient a pu être supprimé pendant qu'un job tournait en tâche de fond
    (le job démarre avant le calcul, peut durer plusieurs minutes) — ne doit
    jamais lever, juste ne rien écrire."""
    seg._persist_segments_to_db("P-DOES-NOT-EXIST", "job-x", [
        {"organ": "kidney_left", "type": "organe", "volume_ml": 140.0},
    ])
    assert _segments_for("P-DOES-NOT-EXIST", db_session_factory) == []


def test_persist_never_raises_when_db_unavailable(monkeypatch):
    """Un échec de persistance (DB down, verrou SQLite, etc.) ne doit JAMAIS faire
    échouer le job de segmentation lui-même — même principe défensif que
    _maybe_build_mesh pour les échecs de génération de maillage."""
    def _broken_session_local():
        raise RuntimeError("DB indisponible")
    monkeypatch.setattr(seg, "SessionLocal", _broken_session_local)
    seg._persist_segments_to_db("P-ANY", "job-y", [
        {"organ": "kidney_left", "type": "organe", "volume_ml": 140.0},
    ])  # ne doit pas lever
