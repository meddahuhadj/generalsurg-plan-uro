# -*- coding: utf-8 -*-
"""
tests/test_segmentation_capabilities.py — Vérifie que /segmentation/capabilities
reflète honnêtement l'environnement Python réel (pas de valeur figée).

Ne suppose pas que TotalSegmentator soit installé dans l'environnement de test :
vérifie la cohérence interne de la réponse plutôt qu'une valeur attendue fixe,
donc passe aussi bien avec que sans le pipeline de segmentation réel installé.

Lancer : cd backend && pytest tests/test_segmentation_capabilities.py -v
"""
import importlib.util

from fastapi.testclient import TestClient

import main


def _importable(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def test_capabilities_reflects_real_environment():
    client = TestClient(main.app)
    r = client.get("/segmentation/capabilities")
    assert r.status_code == 200
    body = r.json()

    expected_keys = {
        "totalsegmentator", "dicom2nifti", "nibabel", "mesh_export",
        "gpu", "ready_for_real_segmentation", "ready_for_mesh_export",
    }
    assert expected_keys.issubset(body.keys())
    assert all(isinstance(body[k], bool) for k in expected_keys)

    assert body["totalsegmentator"] == _importable("totalsegmentator")
    assert body["dicom2nifti"] == _importable("dicom2nifti")
    assert body["nibabel"] == _importable("nibabel")
    assert body["ready_for_real_segmentation"] == (
        body["totalsegmentator"] and body["dicom2nifti"] and body["nibabel"]
    )
    assert body["ready_for_mesh_export"] == (
        _importable("skimage") and _importable("trimesh") and body["nibabel"]
    )

    # Traçabilité (principe directeur n°6 du cahier des charges : pas de chiffre clinique sans
    # source) : la version installée doit être exposée si et seulement si le paquet l'est.
    if body["totalsegmentator"]:
        assert isinstance(body["totalsegmentator_version"], str) and body["totalsegmentator_version"]
    else:
        assert body["totalsegmentator_version"] is None
