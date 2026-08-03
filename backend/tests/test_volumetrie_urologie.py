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
from types import SimpleNamespace

import pytest

from routers.volumetrie import _pick_urologie_kidney_volume, _renal_nephrometry, _resolve_lesion_volume


def _seg(organ, volume_ml, type_="organe"):
    """Simule un models.Segment (ORM) avec un simple objet — _pick_urologie_kidney_volume
    n'accède qu'à .type/.volume_ml/.metadata_json, donc pas besoin d'une vraie DB ici."""
    return SimpleNamespace(type=type_, volume_ml=volume_ml, metadata_json={"organ": organ})


def test_pick_kidney_volume_uses_the_specified_side():
    segs = [_seg("kidney_left", 140.0), _seg("kidney_right", 155.0)]
    vol, source = _pick_urologie_kidney_volume(segs, "left")
    assert vol == 140.0
    assert source == "real_segmentation_kidney_left"


def test_pick_kidney_volume_side_specified_but_not_segmented_falls_back_to_population():
    segs = [_seg("kidney_right", 155.0)]  # rein gauche jamais segmenté (structure absente)
    vol, source = _pick_urologie_kidney_volume(segs, "left")
    assert vol == 0.0
    assert source == "population_estimate"


def test_pick_kidney_volume_single_kidney_present_no_side_needed():
    """Un seul rein segmenté (ex: rein controlatéral non détecté par le modèle) :
    pas d'ambiguïté de côté, utilisable directement sans que l'utilisateur précise."""
    segs = [_seg("kidney_right", 155.0)]
    vol, source = _pick_urologie_kidney_volume(segs, None)
    assert vol == 155.0
    assert source == "real_segmentation_kidney_right"


def test_pick_kidney_volume_never_guesses_when_both_kidneys_ambiguous():
    """Décision clinique explicite requise : quand les deux reins sont segmentés
    et qu'aucun côté n'est précisé, ne JAMAIS deviner lequel est opéré — retomber
    sur l'estimation de population (comportement historique) plutôt qu'un choix
    silencieux qui fausserait le DFG prédit post-opératoire."""
    segs = [_seg("kidney_left", 140.0), _seg("kidney_right", 155.0)]
    vol, source = _pick_urologie_kidney_volume(segs, None)
    assert vol == 0.0
    assert source == "population_estimate"


def test_pick_kidney_volume_ignores_other_organe_segments():
    """La vessie et les surrénales sont aussi type=="organe" en urologie — ne
    doivent jamais être confondues avec un volume rénal."""
    segs = [_seg("urinary_bladder", 300.0), _seg("adrenal_gland_left", 8.0), _seg("kidney_left", 140.0)]
    vol, source = _pick_urologie_kidney_volume(segs, "left")
    assert vol == 140.0


def test_pick_kidney_volume_ignores_lesion_type_segments():
    segs = [_seg("kidney_left_tumor", 20.0, type_="lesion"), _seg("kidney_left", 140.0)]
    vol, source = _pick_urologie_kidney_volume(segs, "left")
    assert vol == 140.0


def _seg_with_source(organ, volume_ml, type_="lesion", ai=True):
    meta = {"organ": organ}
    if ai:
        meta["source"] = "ai_segmentation"
    return SimpleNamespace(type=type_, volume_ml=volume_ml, metadata_json=meta)


def test_resolve_lesion_volume_real_zero_is_not_overwritten_by_population_constant():
    """Le vrai bug corrigé dans cette session : une segmentation IA a tourné (reins
    persistés) et n'a trouvé AUCUN kyste — lesion_vol=0.0 est un résultat clinique réel,
    pas une absence de donnée. Ne doit JAMAIS être remplacé par la constante 20.0 mL."""
    segs = [_seg_with_source("kidney_left", 140.0, type_="organe", ai=True)]  # pas de kyste
    vol, source = _resolve_lesion_volume(segs)
    assert vol == 0.0
    assert source == "real_segmentation"


def test_resolve_lesion_volume_no_ai_segmentation_falls_back_to_population_constant():
    """Aucune segmentation IA n'a jamais tourné pour ce patient : lesion_vol=0.0 signifie
    ici "pas de donnée", pas "pas de lésion" — le comportement historique (estimation de
    20.0 mL) doit être préservé."""
    vol, source = _resolve_lesion_volume([])
    assert vol == 20.0
    assert source == "population_estimate"


def test_resolve_lesion_volume_real_kidney_cyst_detected():
    segs = [
        _seg_with_source("kidney_left", 140.0, type_="organe", ai=True),
        _seg_with_source("kidney_cyst_left", 4.5, type_="lesion", ai=True),
    ]
    vol, source = _resolve_lesion_volume(segs)
    assert vol == pytest.approx(4.5)
    assert source == "real_segmentation"


def test_resolve_lesion_volume_manual_segment_without_ai_flag_still_uses_population_fallback_if_zero():
    """Un segment saisi manuellement (pas de metadata.source="ai_segmentation") ne compte
    pas comme preuve qu'une segmentation IA a tourné — cohérent avec _persist_segments_to_db
    qui ne marque QUE les segments IA de cette façon."""
    segs = [SimpleNamespace(type="organe", volume_ml=99.0, metadata_json={})]  # saisie manuelle
    vol, source = _resolve_lesion_volume(segs)
    assert vol == 20.0
    assert source == "population_estimate"


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
