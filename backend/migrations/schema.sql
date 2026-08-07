-- GeneralSurg Plan MIMO — Schéma PostgreSQL
-- ======================================
-- Exécuter avec: psql -U postgres -d generalsurg -f schema.sql
-- (ou laisser main.py le faire automatiquement au démarrage / via Alembic)

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Table: users (chirurgiens, anesthésistes, etc.) — avec support 2FA (TOTP)
CREATE TABLE users (
    id                   SERIAL PRIMARY KEY,
    username             VARCHAR(64) UNIQUE NOT NULL,
    full_name            VARCHAR(128) NOT NULL,
    email                VARCHAR(256) UNIQUE,
    role                 VARCHAR(32) NOT NULL DEFAULT 'surgeon',
    hashed_password      TEXT NOT NULL,
    rpps                 VARCHAR(32),
    is_active            BOOLEAN DEFAULT TRUE,
    totp_secret          VARCHAR(64),              -- secret actif (2FA activée)
    totp_pending_secret  VARCHAR(64),               -- secret en attente de confirmation
    totp_enabled         BOOLEAN DEFAULT FALSE,
    totp_recovery_codes  JSONB DEFAULT '[]',        -- codes de secours à usage unique (hashés)
    last_login_at        TIMESTAMPTZ,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    updated_at           TIMESTAMPTZ DEFAULT NOW()
);

-- Table: patients
CREATE TABLE patients (
    id              VARCHAR(32) PRIMARY KEY,
    nom             VARCHAR(128) NOT NULL,
    age             INTEGER CHECK (age >= 0 AND age <= 150),
    sexe            CHAR(1) CHECK (sexe IN ('M', 'F')),
    poids_kg        REAL CHECK (poids_kg > 0 AND poids_kg <= 500),
    taille_cm       REAL CHECK (taille_cm > 30 AND taille_cm <= 250),
    bsa_m2          REAL GENERATED ALWAYS AS (SQRT(poids_kg * taille_cm / 3600)) STORED,
    diagnostic      TEXT NOT NULL,
    chirurgien      VARCHAR(128) NOT NULL,
    specialty       VARCHAR(32) NOT NULL DEFAULT 'hbp'
                    CHECK (specialty IN ('hbp','colorectal','gastrique','thyroide','thoracique','cardiaque','urologie')),
    urgence         VARCHAR(16) DEFAULT 'vert' CHECK (urgence IN ('vert','orange','rouge')),
    note            TEXT,
    status          VARCHAR(32) DEFAULT 'active',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Table: sessions (contexte chirurgical en cours)
CREATE TABLE sessions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id      VARCHAR(32) REFERENCES patients(id) ON DELETE CASCADE,
    user_id         INTEGER REFERENCES users(id),
    type            VARCHAR(64),
    statut          VARCHAR(32) DEFAULT 'open',
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    locked          BOOLEAN DEFAULT FALSE,
    CONSTRAINT chk_sessions CHECK (ended_at IS NULL OR ended_at >= started_at)
);

-- Table: segments (volumes segmentés — organes, lésions, résections, structures tubulaires)
CREATE TABLE segments (
    id              VARCHAR(64) PRIMARY KEY,
    session_id      UUID REFERENCES sessions(id) ON DELETE CASCADE,
    patient_id      VARCHAR(32) REFERENCES patients(id) ON DELETE CASCADE,
    type            VARCHAR(32) NOT NULL CHECK (type IN ('organe','lesion','resection','structure_tubulaire','ganglion')),
    volume_ml       REAL NOT NULL CHECK (volume_ml >= 0),
    label           VARCHAR(128),
    color_hex       VARCHAR(7) DEFAULT '#ff0000',
    mesh_ref        TEXT,               -- chemin/URL du maillage STL/GLB réel (segmentation)
    slice_refs      JSONB,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Table: dicom_series
CREATE TABLE dicom_series (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id          VARCHAR(32) REFERENCES patients(id) ON DELETE CASCADE,
    session_id          UUID REFERENCES sessions(id) ON DELETE SET NULL,
    study_uid           VARCHAR(256) NOT NULL,
    series_uid          VARCHAR(256) UNIQUE NOT NULL,
    modality            VARCHAR(8) CHECK (modality IN ('CT','MR','PT','US')),
    manufacturer        VARCHAR(128),
    model               VARCHAR(128),
    slice_thickness_mm  REAL,
    rows                INTEGER,
    cols                INTEGER,
    num_slices          INTEGER,
    pixel_spacing       REAL[],
    window_center       REAL,
    window_width        REAL,
    sha256              CHAR(16),
    size_bytes          BIGINT,
    file_path           TEXT,
    imported_at         TIMESTAMPTZ DEFAULT NOW()
);

-- Table: volumetrie_results
CREATE TABLE volumetrie_results (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id          UUID REFERENCES sessions(id),
    patient_id          VARCHAR(32) REFERENCES patients(id),
    organ_volume_ml     REAL NOT NULL,
    lesion_volume_ml    REAL NOT NULL,
    ratio_lesion_organe_pct REAL,
    volume_resection_ml REAL,
    remnant_pct         REAL NOT NULL,
    flr_threshold_pct   REAL,
    flr_safe            BOOLEAN,
    flr_bw_pct          REAL,
    bsa_m2              REAL,
    margin_cm           REAL DEFAULT 1.0,
    is_cirrhotic        BOOLEAN DEFAULT FALSE,
    computed_at         TIMESTAMPTZ DEFAULT NOW()
);

-- Table: plans_coupe
CREATE TABLE plans_coupe (
    id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id            UUID REFERENCES sessions(id) ON DELETE CASCADE,
    patient_id            VARCHAR(32) REFERENCES patients(id),
    type                  VARCHAR(64) NOT NULL,
    plane_normal          REAL[] NOT NULL,
    plane_point           REAL[] NOT NULL,
    distance_tumor_mm     REAL,
    distance_vaisseau_mm  REAL,
    volume_resected_ml    REAL,
    remnant_pct           REAL,
    validated             BOOLEAN DEFAULT FALSE,
    validated_by          INTEGER REFERENCES users(id),
    validated_at          TIMESTAMPTZ,
    metadata              JSONB DEFAULT '{}',
    created_at            TIMESTAMPTZ DEFAULT NOW()
);

-- Table: audit_log — traçabilité complète (qui, quand, quoi, sur quel patient)
CREATE TABLE audit_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         INTEGER REFERENCES users(id),
    username        VARCHAR(64),          -- dénormalisé : reste lisible même si l'utilisateur est supprimé
    patient_id      VARCHAR(32),          -- pas de FK stricte : on veut garder la trace même si le patient est supprimé
    action          VARCHAR(256) NOT NULL,
    resource        VARCHAR(64),          -- ex: 'patient', 'segment', 'dicom', 'auth', 'export'
    method          VARCHAR(8),           -- verbe HTTP
    path            VARCHAR(256),
    status_code     INTEGER,
    ip_address      VARCHAR(64),
    niveau          VARCHAR(16) CHECK (niveau IN ('info','ok','warn','error')) DEFAULT 'info',
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Table: export_history
CREATE TABLE export_history (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id      UUID REFERENCES sessions(id),
    patient_id      VARCHAR(32) REFERENCES patients(id),
    user_id         INTEGER REFERENCES users(id),
    format          VARCHAR(16) CHECK (format IN ('pdf','json','dicom-sr','dicom-rt')),
    file_path       TEXT,
    file_hash       CHAR(64),
    file_size_bytes BIGINT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_patients_status ON patients(status);
CREATE INDEX idx_patients_specialty ON patients(specialty);
CREATE INDEX idx_segments_patient ON segments(patient_id);
CREATE INDEX idx_segments_session ON segments(session_id);
CREATE INDEX idx_dicom_patient ON dicom_series(patient_id);
CREATE INDEX idx_dicom_study ON dicom_series(study_uid);
CREATE INDEX idx_sessions_patient ON sessions(patient_id);
CREATE INDEX idx_volumetrie_session ON volumetrie_results(session_id);
CREATE INDEX idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX idx_audit_user ON audit_log(user_id);
CREATE INDEX idx_audit_patient ON audit_log(patient_id);
CREATE INDEX idx_plans_session ON plans_coupe(session_id);
CREATE INDEX idx_exports_session ON export_history(session_id);

-- Triggers updated_at
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER patients_updated_at BEFORE UPDATE ON patients
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ==============================================================================
-- TABLES GENERALSURG PLAN 3D NEXTGEN (v2.0 - 2026-2046)
-- Conformité : HIPAA / RGPD / MDR 2017/745 / IEC 62304 Classe C
-- ==============================================================================

-- Table: digital_twins (Jumeaux Numériques 3D & Biophysiques)
CREATE TABLE digital_twins (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_id           VARCHAR(32) NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    source_series_id     UUID REFERENCES dicom_series(id) ON DELETE SET NULL,
    version              VARCHAR(32) NOT NULL DEFAULT 'v2.0-nextgen',
    status               VARCHAR(32) NOT NULL DEFAULT 'READY' CHECK (status IN ('PROCESSING', 'READY', 'ARCHIVED', 'ERROR')),
    organ_target         VARCHAR(64) NOT NULL DEFAULT 'HBP',
    mesh_storage_uri     JSONB NOT NULL DEFAULT '{}'::jsonb,
    biophysical_props    JSONB NOT NULL DEFAULT '{}'::jsonb,
    vascular_graph_json  JSONB,
    volumetric_metrics   JSONB,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_digital_twins_patient ON digital_twins(patient_id);
CREATE TRIGGER digital_twins_updated_at BEFORE UPDATE ON digital_twins
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Table: surgical_plans (Plans Chirurgicaux & Check-list IA)
CREATE TABLE surgical_plans (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    twin_id                 UUID NOT NULL REFERENCES digital_twins(id) ON DELETE CASCADE,
    patient_id              VARCHAR(32) NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    lead_surgeon_username   VARCHAR(64) NOT NULL,
    title                   VARCHAR(256) NOT NULL,
    specialty               VARCHAR(64) NOT NULL DEFAULT 'HBP',
    planned_procedure_code  VARCHAR(64) NOT NULL DEFAULT 'CCAM-HMFA004',
    strategy_status         VARCHAR(32) NOT NULL DEFAULT 'AI_PROPOSED' CHECK (strategy_status IN ('DRAFT', 'AI_PROPOSED', 'APPROVED', 'IN_PROGRESS', 'COMPLETED', 'ABORTED')),
    resection_volume_ml     REAL,
    remnant_volume_ml       REAL,
    remnant_ratio_pct       REAL,
    estimated_blood_loss_ml REAL,
    estimated_duration_min  INTEGER,
    safety_margins_mm       REAL NOT NULL DEFAULT 5.0,
    ai_risk_score           REAL,
    ai_shap_explanations    JSONB,
    preop_checklist_status  JSONB NOT NULL DEFAULT '{"all_cleared": false, "warnings": []}'::jsonb,
    -- Validation clinique explicite (migration c3d4e5f6a7b8) : renseignées uniquement par
    -- POST /plans/{id}/approve (rôle surgeon/admin) et /abort (motif obligatoire), jamais
    -- par une mise à jour générique du statut.
    approved_by_username    VARCHAR(64),
    approved_at             TIMESTAMPTZ,
    aborted_by_username     VARCHAR(64),
    aborted_at              TIMESTAMPTZ,
    abort_reason            TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_surgical_plans_patient ON surgical_plans(patient_id);
CREATE INDEX idx_surgical_plans_status ON surgical_plans(strategy_status);
CREATE TRIGGER surgical_plans_updated_at BEFORE UPDATE ON surgical_plans
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Table: audit_logs (Journal d'Audit Inaltérable - Conformité MDR/HIPAA avec chaînage SHA-256)
CREATE TABLE audit_logs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    timestamp_utc       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id             INTEGER,
    username            VARCHAR(64),
    user_role           VARCHAR(64),
    ip_address          VARCHAR(64),
    action_type         VARCHAR(64) NOT NULL,
    target_resource     VARCHAR(128) NOT NULL,
    resource_id         VARCHAR(64),
    details             JSONB NOT NULL DEFAULT '{}'::jsonb,
    cryptographic_hash  VARCHAR(64) NOT NULL,
    prev_log_hash       VARCHAR(64)
);

CREATE INDEX idx_audit_logs_timestamp ON audit_logs(timestamp_utc DESC);
CREATE INDEX idx_audit_logs_action ON audit_logs(action_type);

-- Seed: utilisateurs de démonstration (mot de passe: changeme)
-- À NE JAMAIS UTILISER EN PRODUCTION — créez de vrais comptes avant mise en service.
-- INSERT INTO users (username, full_name, role, hashed_password)
-- VALUES ('dr.hadj', 'Dr. Hadj', 'surgeon', crypt('changeme', gen_salt('bf')));

