# -*- coding: utf-8 -*-
"""
routers/plans.py — Plans chirurgicaux persistés (pas de faux « travail en cours »).

Endpoints exposés :
    GET    /patients/{patient_id}/plans          Liste des plans du patient
    POST   /patients/{patient_id}/plans          Création d'un plan (statut initial)
    GET    /plans/{plan_id}                      Détail d'un plan
    PUT    /plans/{plan_id}                      Mise à jour (y compris statut)
    DELETE /plans/{plan_id}                      Suppression d'un plan

Le workflow de statut suit le cycle clinique réel et n'autorise QUE les
transitions valides (une opération ne « saute » pas d'APPROVED vers
COMPLETED sans passer par IN_PROGRESS, etc.) :

    DRAFT ──────► APPROVED ──────► IN_PROGRESS ──────► COMPLETED
       │              │                 │
       └── ABORTED ───┴──── ABORTED ────┘

    AI_PROPOSED ──► APPROVED / ABORTED     (réservé : aucun pipeline IA réel
                                             ne propose encore de plans ici)

Les champs `ai_risk_score` / `ai_shap_explanations` du modèle restent NULL :
aucune source de vérité réelle ne les alimente (voir models.SurgicalPlan).

Le champ `metadata_json` porte les volumes réels calculés (organe/lésion), la
marge mesurée, et la source de vérité (« real_segmentation » vs
« population_estimate ») pour que le statut d'un plan reste honnête.
"""

from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

import models
from db import get_db
from deps import get_current_user, require_role, write_audit
from schemas import (
    PlanStatus, SurgicalPlanAbort, SurgicalPlanApprove, SurgicalPlanCreate,
    SurgicalPlanOut, SurgicalPlanUpdate,
)
from specialties import Specialty

router = APIRouter(tags=["plans"])

# Transitions de statut autorisées (diagramme en docstring du module).
_ALLOWED_TRANSITIONS: Dict[str, set] = {
    "DRAFT": {"APPROVED", "ABORTED"},
    "AI_PROPOSED": {"APPROVED", "ABORTED"},
    "APPROVED": {"IN_PROGRESS", "ABORTED"},
    "IN_PROGRESS": {"COMPLETED", "ABORTED"},
    "COMPLETED": set(),
    "ABORTED": set(),
}

# Transitions cliniquement engageantes : elles n'ont le droit de se produire QUE via les
# endpoints dédiés ci-dessous (POST .../approve, POST .../abort), jamais via le PUT générique.
# Raison : APPROVED sanctionne qu'un chirurgien a personnellement revu le plan avant de
# l'engager (rôle vérifié + identité + horodatage dédiés) ; ABORTED doit toujours porter un
# motif tracé. Un PUT générique ne peut vérifier ni l'un ni l'autre.
_GATED_TRANSITIONS = {"APPROVED", "ABORTED"}


def _plan_out(p: models.SurgicalPlan) -> SurgicalPlanOut:
    return SurgicalPlanOut(
        id=p.id,
        patient_id=p.patient_id,
        twin_id=p.twin_id,
        lead_surgeon_username=p.lead_surgeon_username,
        title=p.title,
        specialty=p.specialty,
        planned_procedure_code=p.planned_procedure_code,
        strategy_status=p.strategy_status,
        safety_margins_mm=p.safety_margins_mm,
        resection_volume_ml=p.resection_volume_ml,
        remnant_volume_ml=p.remnant_volume_ml,
        remnant_ratio_pct=p.remnant_ratio_pct,
        estimated_blood_loss_ml=p.estimated_blood_loss_ml,
        estimated_duration_min=p.estimated_duration_min,
        preop_checklist_status=p.preop_checklist_status or {},
        metadata_json=p.metadata_json or {},
        approved_by_username=p.approved_by_username,
        approved_at=p.approved_at,
        aborted_by_username=p.aborted_by_username,
        aborted_at=p.aborted_at,
        abort_reason=p.abort_reason,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _get_patient_or_404(db: Session, patient_id: str) -> models.Patient:
    p = db.get(models.Patient, patient_id)
    if not p:
        raise HTTPException(404, "Patient introuvable.")
    return p


@router.get("/patients/{patient_id}/plans", response_model=List[SurgicalPlanOut])
async def list_plans(patient_id: str,
                     current: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_patient_or_404(db, patient_id)
    rows = (db.query(models.SurgicalPlan)
              .filter(models.SurgicalPlan.patient_id == patient_id)
              .order_by(models.SurgicalPlan.created_at.desc())
              .all())
    return [_plan_out(p) for p in rows]


@router.post("/patients/{patient_id}/plans", response_model=SurgicalPlanOut, status_code=201)
async def create_plan(patient_id: str, payload: SurgicalPlanCreate, request: Request,
                      current: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    _get_patient_or_404(db, patient_id)
    rec = models.SurgicalPlan(
        patient_id=patient_id,
        lead_surgeon_username=current.username,
        strategy_status=payload.strategy_status,
        title=payload.title,
        specialty=payload.specialty,
        planned_procedure_code=payload.planned_procedure_code,
        safety_margins_mm=payload.safety_margins_mm,
        resection_volume_ml=payload.resection_volume_ml,
        remnant_volume_ml=payload.remnant_volume_ml,
        remnant_ratio_pct=payload.remnant_ratio_pct,
        estimated_blood_loss_ml=payload.estimated_blood_loss_ml,
        estimated_duration_min=payload.estimated_duration_min,
        metadata_json=payload.metadata_json,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    write_audit(db, request, f"Création plan chirurgical ({payload.title})",
                "surgical_plan", user=current, patient_id=patient_id,
                niveau="ok", metadata={"plan_id": rec.id, "status": rec.strategy_status})
    return _plan_out(rec)


@router.get("/plans/{plan_id}", response_model=SurgicalPlanOut)
async def get_plan(plan_id: str, request: Request,
                   current: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    rec = db.get(models.SurgicalPlan, plan_id)
    if not rec:
        raise HTTPException(404, "Plan introuvable.")
    write_audit(db, request, "Consultation plan chirurgical", "surgical_plan",
                user=current, patient_id=rec.patient_id)
    return _plan_out(rec)


@router.put("/plans/{plan_id}", response_model=SurgicalPlanOut)
async def update_plan(plan_id: str, payload: SurgicalPlanUpdate, request: Request,
                      current: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    rec = db.get(models.SurgicalPlan, plan_id)
    if not rec:
        raise HTTPException(404, "Plan introuvable.")

    updates = payload.model_dump(exclude_unset=True)

    # Validation du changement de statut : seule transition autorisée.
    new_status = updates.get("strategy_status")
    if new_status is not None and new_status != rec.strategy_status:
        allowed = _ALLOWED_TRANSITIONS.get(rec.strategy_status, set())
        if new_status not in allowed:
            raise HTTPException(
                409,
                f"Transition de statut refusée : {rec.strategy_status} -> {new_status} "
                f"(autorisé : {sorted(allowed)}).",
            )
        if new_status in _GATED_TRANSITIONS:
            endpoint = "approve" if new_status == "APPROVED" else "abort"
            raise HTTPException(
                409,
                f"Transition vers {new_status} refusée via PUT générique : cette étape engage "
                f"une décision clinique et doit passer par POST /plans/{{id}}/{endpoint} "
                f"(rôle vérifié, identité et motif tracés).",
            )

    # un plan ABORTED ou COMPLETED est figé (traçabilité clinique).
    if rec.strategy_status in ("COMPLETED", "ABORTED") and new_status is None:
        editable = [k for k in updates if k != "strategy_status"]
        if editable:
            raise HTTPException(409, f"Plan {rec.strategy_status} : modification refusée (figé).")

    for k, v in updates.items():
        setattr(rec, k, v)
    db.commit()
    db.refresh(rec)
    write_audit(db, request, f"Mise à jour plan chirurgical ({rec.title})", "surgical_plan",
                user=current, patient_id=rec.patient_id,
                niveau="ok", metadata={"plan_id": rec.id, "updated_fields": list(updates.keys()),
                                       "status": rec.strategy_status})
    return _plan_out(rec)


@router.post("/plans/{plan_id}/approve", response_model=SurgicalPlanOut)
async def approve_plan(plan_id: str, payload: SurgicalPlanApprove, request: Request,
                       current: models.User = Depends(require_role("surgeon", "admin")),
                       db: Session = Depends(get_db)):
    """Validation clinique explicite d'un plan (DRAFT/AI_PROPOSED -> APPROVED).

    Réservé aux rôles surgeon/admin (require_role) — un profil non clinique (ex. secrétariat,
    si un tel rôle existe un jour) ne peut pas engager un plan chirurgical. `confirmation` doit
    être `true` : on n'accepte pas un simple changement de statut, on exige un acte volontaire
    distinct. L'identité de l'approbateur et l'horodatage sont persistés sur le plan lui-même
    (pas seulement dans l'audit trail) pour rester visibles à l'écran sans requête séparée.
    """
    rec = db.get(models.SurgicalPlan, plan_id)
    if not rec:
        raise HTTPException(404, "Plan introuvable.")
    if not payload.confirmation:
        raise HTTPException(422, "confirmation doit être `true` pour valider un plan.")
    allowed = _ALLOWED_TRANSITIONS.get(rec.strategy_status, set())
    if "APPROVED" not in allowed:
        raise HTTPException(
            409,
            f"Transition refusée : {rec.strategy_status} -> APPROVED "
            f"(autorisé depuis ce statut : {sorted(allowed) or 'aucune'}).",
        )

    # Source des données : un plan fondé sur une estimation de population (pas une segmentation
    # réelle du patient) reste approuvable — la décision finale appartient au chirurgien — mais
    # ce fait doit être visible dans l'audit de l'approbation, pas seulement dans metadata_json.
    # Convention déjà utilisée côté frontend (renderPlanCard) : tout `volume_source` commençant
    # par "real" est une mesure réelle (ex. "real_segmentation_totalsegmentator"), le reste est
    # une estimation non clinique.
    volume_source = (rec.metadata_json or {}).get("volume_source")
    is_real_source = bool(volume_source) and str(volume_source).startswith("real")

    rec.strategy_status = "APPROVED"
    rec.approved_by_username = current.username
    rec.approved_at = datetime.utcnow()
    db.commit()
    db.refresh(rec)
    write_audit(
        db, request, f"Validation clinique du plan ({rec.title})", "surgical_plan",
        user=current, patient_id=rec.patient_id,
        niveau="ok" if is_real_source else "warn",
        metadata={
            "plan_id": rec.id, "status": "APPROVED",
            "volume_source": volume_source,
            "comment": payload.comment,
        },
    )
    return _plan_out(rec)


@router.post("/plans/{plan_id}/abort", response_model=SurgicalPlanOut)
async def abort_plan(plan_id: str, payload: SurgicalPlanAbort, request: Request,
                     current: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Abandon d'un plan, avec motif tracé obligatoire (`reason`, min. 3 caractères).

    Contrairement à l'approbation, l'abandon n'est pas restreint aux rôles cliniques : toute
    personne autorisée à consulter le plan peut avoir besoin de l'annuler (changement de
    stratégie, contre-indication découverte, etc.) — mais jamais sans laisser de trace du
    pourquoi, ce que le PUT générique ne garantissait pas.
    """
    rec = db.get(models.SurgicalPlan, plan_id)
    if not rec:
        raise HTTPException(404, "Plan introuvable.")
    allowed = _ALLOWED_TRANSITIONS.get(rec.strategy_status, set())
    if "ABORTED" not in allowed:
        raise HTTPException(
            409,
            f"Transition refusée : {rec.strategy_status} -> ABORTED "
            f"(autorisé depuis ce statut : {sorted(allowed) or 'aucune'}).",
        )

    rec.strategy_status = "ABORTED"
    rec.aborted_by_username = current.username
    rec.aborted_at = datetime.utcnow()
    rec.abort_reason = payload.reason
    db.commit()
    db.refresh(rec)
    write_audit(
        db, request, f"Abandon du plan ({rec.title}) : {payload.reason}", "surgical_plan",
        user=current, patient_id=rec.patient_id, niveau="warn",
        metadata={"plan_id": rec.id, "status": "ABORTED", "reason": payload.reason},
    )
    return _plan_out(rec)


@router.delete("/plans/{plan_id}")
async def delete_plan(plan_id: str, request: Request,
                      current: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    rec = db.get(models.SurgicalPlan, plan_id)
    if not rec:
        raise HTTPException(404, "Plan introuvable.")
    title = rec.title
    db.delete(rec)
    db.commit()
    write_audit(db, request, f"Suppression plan chirurgical ({title})", "surgical_plan",
                user=current, patient_id=rec.patient_id, niveau="warn", metadata={"plan_id": plan_id})
    return {"deleted": plan_id}
