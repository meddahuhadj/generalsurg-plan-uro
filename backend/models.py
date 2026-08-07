# -*- coding: utf-8 -*-
"""
models.py — Modèles SQLAlchemy ORM (miroir de migrations/schema.sql).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text, DateTime, ForeignKey, JSON
)
from sqlalchemy.orm import relationship

from db import Base


def _uuid():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    full_name = Column(String(128), nullable=False)
    email = Column(String(256), unique=True, nullable=True)
    role = Column(String(32), nullable=False, default="surgeon")
    hashed_password = Column(Text, nullable=False)
    rpps = Column(String(32), nullable=True)
    is_active = Column(Boolean, default=True)

    # 2FA (TOTP)
    totp_secret = Column(String(64), nullable=True)            # actif une fois activé
    totp_pending_secret = Column(String(64), nullable=True)    # en attente de confirmation
    totp_enabled = Column(Boolean, default=False)
    totp_recovery_codes = Column(JSON, default=list)           # codes de secours (hashés)

    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Patient(Base):
    __tablename__ = "patients"

    id = Column(String(32), primary_key=True)
    nom = Column(String(128), nullable=False)
    age = Column(Integer, nullable=False)
    sexe = Column(String(1), nullable=False)
    poids_kg = Column(Float, nullable=False)
    taille_cm = Column(Float, nullable=False)
    diagnostic = Column(Text, nullable=False)
    chirurgien = Column(String(128), nullable=False)
    specialty = Column(String(32), nullable=False, default="hbp")
    urgence = Column(String(16), default="vert")
    note = Column(Text, nullable=True)
    status = Column(String(32), default="active")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    segments = relationship("Segment", back_populates="patient", cascade="all, delete-orphan")
    plans = relationship("SurgicalPlan", back_populates="patient", cascade="all, delete-orphan")

    @property
    def bsa_m2(self):
        if not self.poids_kg or not self.taille_cm:
            return None
        return round((self.poids_kg * self.taille_cm / 3600) ** 0.5, 3)


class Segment(Base):
    __tablename__ = "segments"

    id = Column(String(64), primary_key=True)
    patient_id = Column(String(32), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(32), nullable=False)
    volume_ml = Column(Float, nullable=False)
    label = Column(String(128), nullable=True)
    color_hex = Column(String(7), default="#ff0000")
    mesh_ref = Column(Text, nullable=True)     # chemin/URL du maillage STL/GLB réel
    metadata_json = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)

    patient = relationship("Patient", back_populates="segments")


class DicomSeries(Base):
    __tablename__ = "dicom_series"

    id = Column(String(36), primary_key=True, default=_uuid)
    patient_id = Column(String(32), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    study_uid = Column(String(256), nullable=False)
    series_uid = Column(String(256), unique=True, nullable=False)
    modality = Column(String(8), nullable=False)
    slice_thickness_mm = Column(Float, nullable=True)
    rows = Column(Integer, nullable=True)
    cols = Column(Integer, nullable=True)
    num_slices = Column(Integer, nullable=True)
    sha256 = Column(String(16), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    filename = Column(String(256), nullable=True)
    local_path = Column(String(512), nullable=True)  # dossier disque contenant les fichiers .dcm réels (si sauvegardés)
    imported_at = Column(DateTime, default=datetime.utcnow)


class SurgicalPlan(Base):
    """Plan chirurgical persisté (miroir de la table `surgical_plans` de
    migrations/schema.sql + migrations/versions/b2f3d4e5f6a7).

    Différence assumée avec schema.sql, documentée : `twin_id` y est NOT NULL
    (chaque plan est lié à un jumeau numérique) mais aucune persistance de
    jumeau numérique n'existe encore dans ce dépôt (pas d'endpoint qui écrit
    dans `digital_twins`) — forcer la NOT NULL rendrait impossible la création
    d'un plan. La colonne est donc NULLABLE ici, et remplie le jour où un vrai
    pipeline de jumeau sera branché. `ai_shap_explanations` est pareillement
    déclarée mais JAMAIS peuplée : aucun pipeline SHAP réel n'existe dans ce
    projet, et la colonne ne doit pas servir à simuler une explicabilité IA
    (même principe que le nettoyage des métriques fabriquées — voir README).

    `metadata_json` porte ce que les colonnes typées de la table ne couvrent
    pas : volumes organe/lésion, marge mesurée, source réelle vs estimation,
    staging spécifique par spécialité — voir routers/plans.py.
    """

    __tablename__ = "surgical_plans"

    id = Column(String(36), primary_key=True, default=_uuid)
    twin_id = Column(String(36), nullable=True)  # NULL tant qu'aucun jumeau n'est persisté
    patient_id = Column(String(32), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    lead_surgeon_username = Column(String(64), nullable=False)
    title = Column(String(256), nullable=False)
    specialty = Column(String(64), nullable=False, default="hbp")
    planned_procedure_code = Column(String(64), nullable=False, default="CCAM")
    strategy_status = Column(String(32), nullable=False, default="DRAFT")
    resection_volume_ml = Column(Float, nullable=True)
    remnant_volume_ml = Column(Float, nullable=True)
    remnant_ratio_pct = Column(Float, nullable=True)
    estimated_blood_loss_ml = Column(Float, nullable=True)
    estimated_duration_min = Column(Integer, nullable=True)
    safety_margins_mm = Column(Float, nullable=False, default=5.0)
    ai_risk_score = Column(Float, nullable=True)
    ai_shap_explanations = Column(JSON, nullable=True)  # jamais peuplée (voir docstring)
    preop_checklist_status = Column(JSON, nullable=False, default=lambda: {"all_cleared": False, "warnings": []})
    metadata_json = Column("metadata", JSON, default=dict)

    # Validation clinique explicite — ajoutée suite à l'audit de sécurité clinique : avant
    # ce correctif, n'importe quel utilisateur authentifié (quel que soit son rôle) pouvait
    # faire passer un plan à APPROVED via un simple PUT générique, sans que son identité soit
    # distinguée de celle du créateur ni qu'un rôle clinique soit vérifié. Ces colonnes ne sont
    # renseignées que par les endpoints dédiés POST /plans/{id}/approve et /abort
    # (routers/plans.py), jamais par la mise à jour générique.
    approved_by_username = Column(String(64), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    aborted_by_username = Column(String(64), nullable=True)
    aborted_at = Column(DateTime, nullable=True)
    abort_reason = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    patient = relationship("Patient", back_populates="plans")


class VolumetrieResult(Base):
    __tablename__ = "volumetrie_results"

    id = Column(String(36), primary_key=True, default=_uuid)
    patient_id = Column(String(32), ForeignKey("patients.id"), nullable=False)
    organ_volume_ml = Column(Float, nullable=False)
    lesion_volume_ml = Column(Float, nullable=False)
    ratio_lesion_organe_pct = Column(Float, nullable=True)
    volume_resection_ml = Column(Float, nullable=True)
    remnant_pct = Column(Float, nullable=False)
    flr_threshold_pct = Column(Float, nullable=True)
    flr_safe = Column(Boolean, nullable=True)
    flr_bw_pct = Column(Float, nullable=True)
    bsa_m2 = Column(Float, nullable=True)
    margin_cm = Column(Float, default=1.0)
    is_cirrhotic = Column(Boolean, default=False)
    computed_at = Column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    """Traçabilité complète : qui, quand, quoi, sur quel patient."""
    __tablename__ = "audit_log"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(Integer, nullable=True)
    username = Column(String(64), nullable=True)
    patient_id = Column(String(32), nullable=True)
    action = Column(String(256), nullable=False)
    resource = Column(String(64), nullable=True)
    method = Column(String(8), nullable=True)
    path = Column(String(256), nullable=True)
    status_code = Column(Integer, nullable=True)
    ip_address = Column(String(64), nullable=True)
    niveau = Column(String(16), default="info")
    metadata_json = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
