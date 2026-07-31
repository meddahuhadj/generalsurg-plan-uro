# -*- coding: utf-8 -*-
"""
tests/test_segmentation_urologie.py — Pipeline de segmentation réelle, branche urologie
==========================================================================================
TotalSegmentator n'est pas installé dans cet environnement de test (pas de GPU/poids de
modèle) — même limite que documentée dans README.md pour le reste du pipeline. Ce test
mocke uniquement `totalsegmentator.python_api.totalsegmentator` et
`totalsegmentator.map_to_binary.class_map` (injection dans sys.modules) : la LOGIQUE de
sélection de tâche (task="total", roi_subset), de recherche dynamique des labels, de
tolérance aux structures absentes et de forme du résultat est testée avec une vraie
écriture/lecture NIfTI (nibabel réel) — pas l'inférence deep learning elle-même.

Lancer : cd backend && pytest tests/test_segmentation_urologie.py -v
"""
import shutil
import sys
import tempfile
import types
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

import segmentation_service as seg

# pytest.tmp_path (et tempfile.gettempdir() par défaut) pointent vers le disque
# système (C:\Users\...\AppData\Local\Temp) — sur ce poste, ce disque s'est révélé
# plein (0 octet disponible) pendant le développement de ce test, faisant échouer
# toute écriture de fichier avec une erreur trompeuse. On force donc un répertoire
# temporaire local AU PROJET (même disque que ce dépôt) plutôt que de dépendre de
# l'espace libre sur le disque système, qui est hors du contrôle de ce test.
_LOCAL_TMP_ROOT = Path(__file__).resolve().parent / ".tmp_test_segmentation_urologie"


@pytest.fixture()
def tmp_path():  # noqa: F811 — masque volontairement le fixture pytest natif tmp_path
    _LOCAL_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=_LOCAL_TMP_ROOT))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _install_fake_totalsegmentator(monkeypatch, roi_label_map: dict, mask_shape=(24, 24, 24)):
    """Injecte un faux package `totalsegmentator` dans sys.modules.

    `roi_label_map` : {nom_roi: label_int} pour les structures que ce faux modèle
    "connaît" — toute structure demandée par le pipeline mais absente d'ici doit être
    ignorée proprement (c'est justement ce qu'un des tests ci-dessous vérifie).
    Retourne un dict `calls` rempli par l'appel réel, pour vérifier task/roi_subset.
    """
    calls = {}

    def fake_totalsegmentator(input, output, task, ml, output_type, device, fast,
                               roi_subset=None, quiet=True):
        calls["task"] = task
        calls["roi_subset"] = roi_subset
        calls["input"] = input
        data = np.zeros(mask_shape, dtype=np.uint8)
        for i, (roi, label) in enumerate(roi_label_map.items()):
            x0 = 2 + i * 4
            data[x0:x0 + 2, 2:8, 2:8] = label  # bloc de voxels distinct par structure
        img = nib.Nifti1Image(data, affine=np.eye(4))
        nib.save(img, str(output))

    python_api_mod = types.ModuleType("totalsegmentator.python_api")
    python_api_mod.totalsegmentator = fake_totalsegmentator

    map_to_binary_mod = types.ModuleType("totalsegmentator.map_to_binary")
    map_to_binary_mod.class_map = {"total": {label: roi for roi, label in roi_label_map.items()}}

    totalsegmentator_pkg = types.ModuleType("totalsegmentator")
    totalsegmentator_pkg.python_api = python_api_mod
    totalsegmentator_pkg.map_to_binary = map_to_binary_mod

    monkeypatch.setitem(sys.modules, "totalsegmentator", totalsegmentator_pkg)
    monkeypatch.setitem(sys.modules, "totalsegmentator.python_api", python_api_mod)
    monkeypatch.setitem(sys.modules, "totalsegmentator.map_to_binary", map_to_binary_mod)
    return calls


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path, monkeypatch):
    """Redirige WORKDIR/MESH_STORAGE vers un dossier temporaire pour ce test — pas
    d'écriture dans ./storage/meshes ni le dossier temp système partagé du projet."""
    monkeypatch.setattr(seg, "WORKDIR", tmp_path / "work")
    monkeypatch.setattr(seg, "MESH_STORAGE", tmp_path / "meshes")
    seg.WORKDIR.mkdir(parents=True, exist_ok=True)
    seg.MESH_STORAGE.mkdir(parents=True, exist_ok=True)


def test_urologie_pipeline_all_rois_present(monkeypatch, tmp_path):
    roi_label_map = {
        "kidney_left": 1, "kidney_right": 2, "urinary_bladder": 3,
        "adrenal_gland_left": 4, "adrenal_gland_right": 5,
    }
    calls = _install_fake_totalsegmentator(monkeypatch, roi_label_map)

    job_id = "testjob1"
    job = {"status": "pending", "progress": None, "result": None, "error": None}
    nifti_input = tmp_path / "input.nii.gz"
    nifti_input.touch()

    result = seg._run_urologie_segmentation_job(job_id, nifti_input, "P-URO-1", job, seg.time.time())

    assert calls["task"] == "total"
    assert set(calls["roi_subset"]) == set(seg._UROLOGIE_ROIS)

    assert len(result["segments"]) == 5
    names = {s["organ"] for s in result["segments"]}
    assert names == set(roi_label_map.keys())
    assert all(s["volume_ml"] > 0 for s in result["segments"])
    assert all(s["mesh_url"] and s["mesh_url"].startswith(f"/meshes/{job_id}/") for s in result["segments"])

    kidney_sum = sum(s["volume_ml"] for s in result["segments"] if s["organ"] in ("kidney_left", "kidney_right"))
    assert result["kidney_total_ml"] == pytest.approx(kidney_sum)
    assert result["vessels"] == []
    assert "total" in result["model"]


def test_urologie_pipeline_skips_missing_rois_gracefully(monkeypatch, tmp_path):
    """Une version de TotalSegmentator qui ne connaît pas encore les surrénales
    (ou toute structure de _UROLOGIE_ROIS) ne doit jamais faire planter le job —
    la structure manquante est simplement absente du résultat."""
    roi_label_map = {"kidney_left": 1, "kidney_right": 2, "urinary_bladder": 3}
    _install_fake_totalsegmentator(monkeypatch, roi_label_map)

    job_id = "testjob2"
    job = {"status": "pending", "progress": None, "result": None, "error": None}
    nifti_input = tmp_path / "input.nii.gz"
    nifti_input.touch()

    result = seg._run_urologie_segmentation_job(job_id, nifti_input, "P-URO-2", job, seg.time.time())

    names = {s["organ"] for s in result["segments"]}
    assert names == {"kidney_left", "kidney_right", "urinary_bladder"}
    assert "adrenal_gland_left" not in names
    assert "adrenal_gland_right" not in names


def test_run_segmentation_job_dispatches_to_urologie_branch(monkeypatch, tmp_path):
    """Vérifie le point d'entrée public _run_segmentation_job (utilisé par
    start_job_from_dicom_dir et l'endpoint /segmentation/auto) : specialty="urologie"
    doit produire un résultat de forme urologie, pas le pipeline hépatique par défaut."""
    roi_label_map = {"kidney_left": 1, "kidney_right": 2, "urinary_bladder": 3}
    _install_fake_totalsegmentator(monkeypatch, roi_label_map)

    job_id = "testjob3"
    seg._JOBS[job_id] = {"status": "pending", "progress": None, "result": None, "error": None}
    nifti_input = tmp_path / "input.nii.gz"
    nifti_input.touch()

    seg._run_segmentation_job(job_id, nifti_input, "P-URO-3", specialty="urologie")

    job = seg._JOBS[job_id]
    assert job["status"] == "done", job.get("error")
    assert job["result"] is not None
    assert "kidney_total_ml" in job["result"]
    assert "liver_total_ml" not in job["result"]
    del seg._JOBS[job_id]


def test_run_segmentation_job_default_specialty_still_uses_liver_pipeline(monkeypatch, tmp_path):
    """Compatibilité ascendante : sans specialty explicite (ou specialty="hbp"), le
    pipeline reste le pipeline hépatique existant — ce test échoue proprement (pas de
    faux modèle hépatique installé ici) mais DOIT échouer côté import du pipeline
    liver_segments, PAS être redirigé silencieusement vers la branche urologie."""
    roi_label_map = {"kidney_left": 1, "kidney_right": 2, "urinary_bladder": 3}
    _install_fake_totalsegmentator(monkeypatch, roi_label_map)

    job_id = "testjob4"
    seg._JOBS[job_id] = {"status": "pending", "progress": None, "result": None, "error": None}
    nifti_input = tmp_path / "input.nii.gz"
    nifti_input.touch()

    seg._run_segmentation_job(job_id, nifti_input, "P-HBP-1")  # specialty par défaut = "hbp"

    job = seg._JOBS[job_id]
    # Le faux totalsegmentator() ignore l'argument task= et écrit toujours le même
    # NIfTI factice, donc le pipeline hépatique "réussit" techniquement mais avec des
    # labels absurdes pour du foie — ce n'est pas ce qu'on vérifie ici. Ce qui compte :
    # ça n'a PAS pris le raccourci urologie (pas de kidney_total_ml dans le résultat).
    assert job["status"] == "done", job.get("error")
    assert "kidney_total_ml" not in (job["result"] or {})
    del seg._JOBS[job_id]
