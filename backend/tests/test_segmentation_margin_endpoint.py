# -*- coding: utf-8 -*-
"""
tests/test_segmentation_margin_endpoint.py — GET /segmentation/margin/{job_id}
================================================================================
`segmentation_service.get_oncologic_margin` calcule une distance 3D RÉELLE entre
deux maillages segmentés (voir mesh_export.surface_to_surface_min_distance, déjà
testé unitairement contre des sphères à écart connu dans test_mesh_distance.py) —
mais jusqu'ici jamais exercé au niveau HTTP : rien ne vérifiait le comportement de
l'endpoint lui-même (job introuvable, job pas terminé, structure absente, seuil de
sécurité) avant qu'il soit câblé dans l'UI (voir README, section "Distance 3D
réelle structure-à-structure").

Lancer : cd backend && pytest tests/test_segmentation_margin_endpoint.py -v
"""
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("rtree")

from fastapi.testclient import TestClient

import main
import segmentation_service as seg
from mesh_export import export_mesh_glb


def _sphere(radius, center):
    mesh = trimesh.creation.icosphere(subdivisions=3, radius=radius)
    mesh.apply_translation(center)
    return mesh


@pytest.fixture()
def client():
    return TestClient(main.app)


@pytest.fixture()
def two_structure_job(tmp_path, monkeypatch):
    """Job "done" avec deux vrais maillages GLB (sphères à écart analytiquement connu,
    35.0 mm) — pas de dépendance à TotalSegmentator, même principe que
    test_segmentation_urologie.py : on ne mocke que ce qui nécessite un GPU/des poids
    de modèle, jamais le reste du pipeline (mesh_export réel ici)."""
    monkeypatch.setattr(seg, "MESH_STORAGE", tmp_path)
    job_id = "margin-test-job"
    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    r_a, r_b, d_centers = 10.0, 15.0, 60.0
    expected_gap = d_centers - r_a - r_b  # 35.0 mm
    export_mesh_glb(_sphere(r_a, (0, 0, 0)), job_dir / "kidney_left.glb")
    export_mesh_glb(_sphere(r_b, (d_centers, 0, 0)), job_dir / "kidney_cyst_left.glb")

    seg._JOBS[job_id] = {
        "status": "done", "progress": "Terminé.", "error": None,
        "result": {"segments": []},
        "mesh_info": {"kidney_left": {}, "kidney_cyst_left": {}},
    }
    yield job_id, expected_gap
    del seg._JOBS[job_id]


def test_margin_endpoint_returns_real_measured_distance(client, two_structure_job):
    job_id, expected_gap = two_structure_job
    r = client.get(f"/segmentation/margin/{job_id}?structure_a=kidney_left&structure_b=kidney_cyst_left")
    assert r.status_code == 200
    body = r.json()
    assert body["margin_mm"] == pytest.approx(expected_gap, abs=0.5)
    assert body["structure_a"] == "kidney_left"
    assert body["structure_b"] == "kidney_cyst_left"
    assert body["safety_margin_mm"] is None
    assert body["margin_sufficient"] is None
    assert "direction_a_to_b_mm" in body["details"]
    assert "direction_b_to_a_mm" in body["details"]


def test_margin_endpoint_safety_threshold_sufficient(client, two_structure_job):
    job_id, _ = two_structure_job
    r = client.get(f"/segmentation/margin/{job_id}?structure_a=kidney_left&structure_b=kidney_cyst_left&safety_margin_mm=10")
    body = r.json()
    assert body["margin_sufficient"] is True  # ~35mm >= 10mm


def test_margin_endpoint_safety_threshold_insufficient(client, two_structure_job):
    job_id, _ = two_structure_job
    r = client.get(f"/segmentation/margin/{job_id}?structure_a=kidney_left&structure_b=kidney_cyst_left&safety_margin_mm=50")
    body = r.json()
    assert body["margin_sufficient"] is False  # ~35mm < 50mm


def test_margin_endpoint_unknown_job_404(client):
    r = client.get("/segmentation/margin/does-not-exist?structure_a=a&structure_b=b")
    assert r.status_code == 404


def test_margin_endpoint_job_not_done_yet_409(client):
    seg._JOBS["pending-job"] = {"status": "running", "progress": "...", "error": None, "result": None}
    try:
        r = client.get("/segmentation/margin/pending-job?structure_a=a&structure_b=b")
        assert r.status_code == 409
    finally:
        del seg._JOBS["pending-job"]


def test_margin_endpoint_unknown_structure_404(client, two_structure_job):
    job_id, _ = two_structure_job
    r = client.get(f"/segmentation/margin/{job_id}?structure_a=kidney_left&structure_b=does_not_exist")
    assert r.status_code == 404
