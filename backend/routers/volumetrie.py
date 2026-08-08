# -*- coding: utf-8 -*-
"""
routers/volumetrie.py — Calcul de volumétrie (générique + FLR/TLV spécifique HBP).

Endpoint exposé :
    GET /patients/{patient_id}/volumetrie
"""

import math
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

import models
from db import get_db
from deps import get_current_user, write_audit
from schemas import VolumetrieResponse

router = APIRouter(tags=["volumetrie"])


def _bsa(weight_kg: float, height_cm: float) -> float:
    return (weight_kg * height_cm / 3600) ** 0.5


def _flr_threshold(is_cirrhotic: bool, bsa: float) -> float:
    if is_cirrhotic:
        return max(35.0, 30.0 + 12.0 * (1.0 - bsa / 1.9))
    return max(25.0, 20.0 + 10.0 * (1.0 - bsa / 1.9))


def _pick_urologie_kidney_volume(segments: list, kidney_side: Optional[str]) -> tuple:
    """Sélectionne le volume rénal RÉEL (issu de la segmentation IA, voir
    segmentation_service._persist_segments_to_db) pertinent pour la néphrométrie
    RENAL, qui porte sur UN SEUL rein (celui opéré) — jamais la somme des deux
    reins + vessie + surrénales que donnerait un simple filtre type=="organe".

    Fonction pure (accepte tout objet/dict avec .type / .volume_ml / .metadata_json,
    pas seulement l'ORM SQLAlchemy) pour rester testable sans DB, même principe que
    `_renal_nephrometry` ci-dessous.

    Ne devine JAMAIS quel rein est opéré quand le côté n'est pas précisé ET que les
    deux reins sont segmentés : mieux vaut retomber explicitement sur l'estimation
    de population (volume=0.0, voir get_volumetrie) qu'un choix clinique silencieux
    qui pourrait fausser le DFG prédit post-opératoire.

    Retourne (volume_ml, source) où source décrit l'origine pour l'API/l'audit.
    """
    kidney_by_organ: dict = {}
    for s in segments:
        meta = getattr(s, "metadata_json", None) or {}
        organ = meta.get("organ")
        if getattr(s, "type", None) == "organe" and organ in ("kidney_left", "kidney_right"):
            kidney_by_organ[organ] = s.volume_ml
    if kidney_side:
        vol = kidney_by_organ.get(f"kidney_{kidney_side}")
        if vol:
            return vol, f"real_segmentation_kidney_{kidney_side}"
    elif len(kidney_by_organ) == 1:
        (organ, vol), = kidney_by_organ.items()
        return vol, f"real_segmentation_{organ}"
    return 0.0, "population_estimate"


def _resolve_lesion_volume(segments: list) -> tuple:
    """Volume de lésion (tumeur hépatique, kyste rénal...) : additionne les segments
    type=="lesion" existants, MAIS ne retombe sur la constante historique de 20.0 mL
    que si AUCUNE segmentation IA n'a jamais tourné pour ce patient — sinon un lesion_vol
    réellement nul (ex : aucun kyste rénal détecté par task="kidney_cysts", un résultat
    cliniquement significatif) serait confondu avec "pas de donnée" et écrasé par une
    valeur fictive. Même principe pur/testable que `_pick_urologie_kidney_volume`.

    "Une segmentation IA a tourné" est déterminé par la présence d'AU MOINS UN segment
    marqué metadata.source=="ai_segmentation" (peu importe son type) — indépendant de
    organe_vol, qui peut lui-même retomber sur une estimation par une autre voie.
    """
    has_ai_segmentation = any((getattr(s, "metadata_json", None) or {}).get("source") == "ai_segmentation"
                               for s in segments)
    lesion_vol = sum(s.volume_ml for s in segments if getattr(s, "type", None) == "lesion")
    if lesion_vol == 0 and not has_ai_segmentation:
        return 20.0, "population_estimate"
    return lesion_vol, "real_segmentation" if has_ai_segmentation else "population_estimate"


def _renal_nephrometry(renal_score: Optional[str], dfg_preop: Optional[float],
                        organe_vol: float, lesion_vol: float, margin_cm: float) -> dict:
    """Néphrométrie RENAL (Radius/Exophytic/Nearness/Anterior/Location) → complexité
    tumorale + parenchyme rénal préservé + DFG post-opératoire prédit.

    Fonction pure (aucune dépendance DB/FastAPI), factorisée hors de l'endpoint
    pour rester testable unitairement — même principe que `_flr_threshold`
    ci-dessus. `organe_vol` est toujours > 0 en pratique côté appelant (fallback
    par défaut si aucune segmentation, voir get_volumetrie), donc pas de garde
    contre la division par zéro ici.

    Correctif important : avant ce correctif, le volume réséqué utilisé ici était
    `organe_vol * 0.55 + margin_cm * 32` — une formule générique de résection
    MAJEURE (pensée pour une hépatectomie, où retirer >50% de l'organe est
    courant), silencieusement réutilisée pour une néphrectomie PARTIELLE. Une
    néphrectomie partielle retire la tumeur + une marge de sécurité, presque
    toujours une PETITE fraction du rein : appliquer 55% surestimait massivement
    le volume réséqué, donc sous-estimait le DFG post-opératoire prédit — un
    chiffre que le chirurgien pourrait réellement utiliser pour arbitrer entre
    chirurgie néphron-sparing et néphrectomie totale.
    """
    complexity = None
    if renal_score:
        digits = "".join(ch for ch in renal_score if ch.isdigit())
        score = int(digits) if digits else None
        if score is not None:
            complexity = "simple" if score <= 6 else ("intermediaire" if score <= 9 else "complexe")
    # Volume de parenchyme rénal préservé : en néphrectomie partielle, le résidu fonctionnel
    # est le rein opéré amputé de la résection ; en tumeur complexe (RENAL ≥ 10) orientée
    # néphrectomie totale, seul le rein controlatéral (~50% du DFG total) subsiste.
    if complexity == "complexe":
        preserved_pct = 0.0
        dfg_predicted = round(dfg_preop * 0.5, 1) if dfg_preop else None
    else:
        # Approximation géométrique du volume réséqué en néphrectomie partielle : la lésion
        # + sa marge de sécurité sont modélisées comme une sphère (rayon-équivalent de la
        # lésion + margin_cm). Grossier — une vraie tumeur n'est pas sphérique — mais un ordre
        # de grandeur clinique correct, et honnêtement approximatif plutôt que faussement
        # précis. Plafonné au volume de l'organe (garde-fou, ne devrait jamais être atteint
        # pour une complexité "simple"/"intermédiaire" en pratique).
        r_lesion_cm = (3.0 * max(lesion_vol, 0.0) / (4.0 * math.pi)) ** (1.0 / 3.0)
        r_resected_cm = r_lesion_cm + max(margin_cm, 0.0)
        resected_ml = min(organe_vol * 0.95, (4.0 / 3.0) * math.pi * r_resected_cm ** 3)
        preserved_pct = round((organe_vol - resected_ml) / organe_vol * 100, 1)
        dfg_predicted = round(dfg_preop * preserved_pct / 100, 1) if dfg_preop else None
    return {
        "renal_score": renal_score,
        "renal_complexity": complexity,
        "preserved_parenchyma_pct": preserved_pct,
        "dfg_preop_ml_min": dfg_preop,
        "dfg_predicted_ml_min": dfg_predicted,
    }


@router.get("/patients/{patient_id}/volumetrie", response_model=VolumetrieResponse)
async def get_volumetrie(patient_id: str, request: Request, margin_cm: float = 1.0, is_cirrhotic: bool = False,
                         renal_score: Optional[str] = None, dfg_preop: Optional[float] = None,
                         kidney_side: Optional[Literal["left", "right"]] = None,
                         current: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    p = db.get(models.Patient, patient_id)
    if not p:
        raise HTTPException(404, "Patient introuvable.")
    segments = db.query(models.Segment).filter(models.Segment.patient_id == patient_id).all()
    lesion_vol, lesion_volume_source = _resolve_lesion_volume(segments)

    # En urologie, "organe" désigne à la fois les 2 reins, la vessie et les 2
    # surrénales dans la base — un simple sum() mélangerait ces organes distincts.
    # La néphrométrie RENAL porte sur UN rein : voir _pick_urologie_kidney_volume.
    if p.specialty == "urologie":
        organe_vol, organ_volume_source = _pick_urologie_kidney_volume(segments, kidney_side)
    else:
        organe_vol = sum(s.volume_ml for s in segments if s.type == "organe")
        organ_volume_source = "real_segmentation" if organe_vol > 0 else "population_estimate"

    if organe_vol == 0:
        organe_vol = {"hbp": 1450.0, "colorectal": 350.0, "gastrique": 1100.0, "thyroide": 20.0,
                       "thoracique": 4500.0, "cardiaque": 300.0, "urologie": 150.0}.get(p.specialty, 500.0)

    resected = organe_vol * 0.55 + margin_cm * 32
    remnant_pct = round((organe_vol - resected) / organe_vol * 100, 1)

    result = {
        "patient_id": patient_id, "specialty": p.specialty,
        "organ_volume_ml": round(organe_vol, 1), "lesion_volume_ml": round(lesion_vol, 1),
        "ratio_lesion_organe_pct": round(lesion_vol / organe_vol * 100, 1),
        "volume_resection_ml": round(resected), "remnant_pct": remnant_pct, "margin_cm": margin_cm,
        "organ_volume_source": organ_volume_source, "lesion_volume_source": lesion_volume_source,
    }
    if p.specialty == "hbp":
        bsa_val = _bsa(p.poids_kg, p.taille_cm)
        threshold = round(_flr_threshold(is_cirrhotic, bsa_val), 1)
        result.update({
            "tlv_ml": round(organe_vol, 1), "tv_ml": round(lesion_vol, 1), "flr_pct": remnant_pct,
            "flr_threshold_pct": threshold, "flr_safe": remnant_pct >= threshold,
            "flr_bw_pct": round(remnant_pct * 0.7 / 70, 2), "bsa_m2": round(bsa_val, 2),
        })
    elif p.specialty == "urologie":
        result.update(_renal_nephrometry(renal_score, dfg_preop, organe_vol, lesion_vol, margin_cm))

    db.add(models.VolumetrieResult(
        id=str(uuid.uuid4()), patient_id=patient_id, organ_volume_ml=result["organ_volume_ml"],
        lesion_volume_ml=result["lesion_volume_ml"], ratio_lesion_organe_pct=result["ratio_lesion_organe_pct"],
        volume_resection_ml=result["volume_resection_ml"], remnant_pct=remnant_pct,
        flr_threshold_pct=result.get("flr_threshold_pct"), flr_safe=result.get("flr_safe"),
        flr_bw_pct=result.get("flr_bw_pct"), bsa_m2=result.get("bsa_m2"), margin_cm=margin_cm,
        is_cirrhotic=is_cirrhotic,
    ))
    write_audit(db, request, "Calcul volumétrie", "volumetrie", user=current, patient_id=patient_id)
    return result
