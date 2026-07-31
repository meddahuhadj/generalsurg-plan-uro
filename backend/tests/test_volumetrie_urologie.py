# -*- coding: utf-8 -*-
"""
tests/test_volumetrie_urologie.py — Néphrométrie RENAL (routers/volumetrie.py)
================================================================================
Teste `_renal_nephrometry`, la fonction pure factorisée hors de l'endpoint
GET /patients/{id}/volumetrie (voir routers/volumetrie.py). Pas de DB/HTTP
nécessaire ici — même approche que test_hl7_interop.py (tester la logique
métier directement, sans dépendre du reste de la stack FastAPI).

Lancer : cd backend && pytest tests/test_volumetrie_urologie.py -v
"""
import pytest

from routers.volumetrie import _renal_nephrometry


def test_simple_tumor_score_le_6():
    r = _renal_nephrometry("4a", dfg_preop=90.0, organe_vol=150.0, resected=90.0)
    assert r["renal_complexity"] == "simple"
    # 60 mL préservés sur 150 mL -> 40%
    assert r["preserved_parenchyma_pct"] == pytest.approx(40.0)
    assert r["dfg_predicted_ml_min"] == pytest.approx(90.0 * 0.4, abs=0.1)


def test_intermediate_tumor_score_7_to_9():
    r = _renal_nephrometry("8x", dfg_preop=68.0, organe_vol=150.0, resected=90.0)
    assert r["renal_complexity"] == "intermediaire"
    assert r["preserved_parenchyma_pct"] == pytest.approx(40.0)


def test_complex_tumor_score_ge_10_orients_total_nephrectomy():
    """RENAL >= 10 : le résidu fonctionnel n'est plus le rein opéré (retiré en
    totalité) mais le rein controlatéral, dont le DFG estimé est ~50% du DFG
    total préopératoire — indépendant du volume réséqué localement."""
    r = _renal_nephrometry("11a", dfg_preop=80.0, organe_vol=150.0, resected=90.0)
    assert r["renal_complexity"] == "complexe"
    assert r["preserved_parenchyma_pct"] == 0.0
    assert r["dfg_predicted_ml_min"] == pytest.approx(40.0)


def test_boundary_scores_6_and_7_and_9_and_10():
    assert _renal_nephrometry("6a", None, 150.0, 90.0)["renal_complexity"] == "simple"
    assert _renal_nephrometry("7a", None, 150.0, 90.0)["renal_complexity"] == "intermediaire"
    assert _renal_nephrometry("9x", None, 150.0, 90.0)["renal_complexity"] == "intermediaire"
    assert _renal_nephrometry("10a", None, 150.0, 90.0)["renal_complexity"] == "complexe"


def test_no_renal_score_leaves_complexity_none():
    r = _renal_nephrometry(None, dfg_preop=70.0, organe_vol=150.0, resected=90.0)
    assert r["renal_complexity"] is None
    assert r["renal_score"] is None
    # Pas de score -> traité comme la branche "non complexe" (pas de total
    # nephrectomy présumée) : le parenchyme préservé reste calculé.
    assert r["preserved_parenchyma_pct"] == pytest.approx(40.0)


def test_no_dfg_preop_leaves_prediction_none():
    r = _renal_nephrometry("4a", dfg_preop=None, organe_vol=150.0, resected=90.0)
    assert r["dfg_predicted_ml_min"] is None
    r_complex = _renal_nephrometry("11a", dfg_preop=None, organe_vol=150.0, resected=90.0)
    assert r_complex["dfg_predicted_ml_min"] is None


def test_non_numeric_renal_score_is_ignored_gracefully():
    """Un score mal formé (pas de chiffre extractible) ne doit jamais lever
    d'exception — juste ne pas classer la complexité."""
    r = _renal_nephrometry("abc", dfg_preop=70.0, organe_vol=150.0, resected=90.0)
    assert r["renal_complexity"] is None
