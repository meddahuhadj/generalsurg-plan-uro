# Manuel Technique d'Administration Hospitalière & Ingénierie Biomédicale
## GeneralSurgPlan3D — Architecture & Interopérabilité (PACS / HL7)

**Version :** prototype (non versionné pour un usage clinique)  
**Classification réglementaire : AUCUNE.** Ce logiciel n'a fait l'objet d'AUCUNE certification CE MDR
2017/745, d'AUCUNE évaluation par un organisme notifié, et d'AUCUNE soumission FDA 510(k) — voir
`GET /api/v2/compliance/mdr-fda-status`, qui déclare honnêtement "NOT_CERTIFIED"/"NOT_SUBMITTED".
Une version antérieure de ce document affirmait à tort une "Classification CE MDR Classe IIb/C &
FDA 510(k) Equivalence" : cette affirmation était fabriquée et a été retirée. **Ne pas utiliser en
contexte clinique réel sans engager une démarche réglementaire réelle au préalable.**  
**Cible :** Directeurs des Systèmes d'Information (DSI), Ingénieurs Biomédicaux & Administrateurs PACS hospitaliers — pour évaluation technique et planification d'une éventuelle mise en conformité, pas pour un déploiement clinique en l'état.

---

## 1. Vue d'Ensemble de l'Infrastructure
GeneralSurgPlan3D est conçu comme un micro-écosystème conteneurisé, déployable en environnement de test dans un centre hospitalier (PACS, HL7, base de données). Le schéma ci-dessous décrit l'architecture technique réelle du prototype, pas un déploiement de production certifié.

```
+-----------------------------------------------------------------------------------+
|                         RÉSEAU PRIVÉ HOSPITALIER (VLAN 172.28.0.0/16)             |
|                                                                                   |
|  [Moniteur Anesthésie] (Dräger/Mindray)                                            |
|       │                                                                           |
|       ├─ (IEEE 11073 / HL7 ORU_R01) ──┐                                           |
|       ▼                               ▼                                           |
|  +--------------------+      +--------------------+      +---------------------+  |
|  |   Orthanc PACS     |      |  Backend FastAPI   |      | PostgreSQL + vector |  |
|  |  (DICOMweb Server) |◄────►|  (Jumeaux 3D & IA) |◄────►|  (Audit SHA-256)    |  |
|  |   Port 4242/8042   |      |     Port 8000      |      |      Port 5432      |  |
|  +--------------------+      +--------------------+      +---------------------+  |
|                                       ▲                                           |
|                                       │ (WebGPU / HTTPS WSS TLS 1.3)              |
|                                       ▼                                           |
|                          [Navigateur Stérile au Bloc]                             |
+-----------------------------------------------------------------------------------+
```

### Cybersécurité — état réel, pas un objectif présenté comme acquis
- **Chiffrement au repos :** NON implémenté par le code applicatif lui-même (pas de AES-256-GCM ni
  équivalent dans ce dépôt — vérifiable, aucune occurrence dans `backend/`). Si requis, doit être
  configuré au niveau de l'infrastructure de déploiement (chiffrement de volume PostgreSQL, disque
  chiffré), pas fourni par cette application.
- **Chiffrement en transit :** dépend entièrement de la configuration du reverse proxy/TLS devant
  l'application (non fourni ni imposé par le code applicatif — voir `backend/db.py` / déploiement).
- **Intégrité de l'audit trail :** chaque entrée de `audit_logs` porte un hash **SHA-256** individuel
  (intégrité technique de CETTE entrée), mais il n'y a PAS de chaînage cryptographique vérifié entre
  entrées (pas de type blockchain) ni de détection/alerte automatique en cas d'altération manuelle en
  base. Ne pas présenter cette propriété comme une preuve d'inviolabilité médico-légale.

---

## 2. Configuration PACS (DICOMweb & Orthanc)
Le serveur PACS intégré (Orthanc) communique de manière bidirectionnelle avec les modalités d'imagerie du centre (Scanner multibarrette, IRM 3 Tesla, Arceau 3D).

### Table des AE Titles et Ports
| Composant | AE Title | Adresse IP / Host | Port TCP | Protocole |
| :--- | :--- | :--- | :--- | :--- |
| **Orthanc PACS** | `GENERALSURG_PACS` | `orthanc` (ou IP serveur) | **4242** | DICOM C-STORE / C-FIND |
| **Passerelle WADO** | `GENERALSURG_WEB` | `localhost:8042` | **8042** | HTTP REST / DICOMweb (QIDO-RS / WADO-RS) |
| **Scanner Hospitalier** | `CT_TRAUMA_01` | *À définir par le biomédical* | 104 / 4006 | DICOM C-ECHO / C-STORE |

### Configuration d'importation automatique WADO-RS
Dans le fichier `orthanc.json` de l'hôpital, autorisez les requêtes du routeur PACS de GeneralSurgPlan3D :
```json
{
  "DicomWeb": {
    "Enable": true,
    "Root": "/dicom-web/",
    "EnableWadoRs": true,
    "EnableQidoRs": true
  },
  "RegisteredUsers": {
    "surgadmin": "REMPLACER_PAR_UN_MOT_DE_PASSE_FORT_GENERE"
  }
}
```

---

## 3. Connectivité HL7 v2.x & IEEE 11073 (Moniteurs d'Anesthésie)
Pour alimenter le module peropératoire **🏥 Bloc IA (SurgOR-AI)** et déclencher les alertes d'ischémie de clampage en temps réel :

1. **Protocole de Transport :** MLLP (Minimal Lower Layer Protocol) sur le port TCP `2575` ou flux REST FHIR Observation sur le port HTTPS `8000`.
2. **Types de Messages Reçus :** `ORU^R01` (Unsolicited Transmission of an Observation).
3. **Mapping des Codes LOINC vitales :**
   - Pression Artérielle Systolique/Diastolique : LOINC `8480-6` / `8462-4`
   - Fréquence Cardiaque : LOINC `8867-4`
   - Saturation SpO₂ : LOINC `2708-6`
   - Index Bispectral (BIS Anesthésie) : LOINC `80404-7`

---

## 4. Déploiement (Docker)
Seul `docker-compose.yml` est fourni dans ce dépôt (pas de manifeste Kubernetes) — un déploiement
Kubernetes réel resterait à écrire.

### Démarrage de la stack
```bash
# 1. Cloner le dépôt
cd /opt/generalsurgplan3d

# 2. Lancer l'assemblage et le démarrage des conteneurs
docker compose -f docker-compose.yml up -d --build

# 3. Vérifier la disponibilité HTTP des endpoints (pas une preuve de conformité, voir backend/healthcheck.py)
docker exec -it generalsurg_app python backend/healthcheck.py
```

### Sauvegarde
Sauvegarde cohérente et simultanée de la base de données et des maillages (pas de garantie de
chaînage cryptographique entre entrées, voir plus haut) :
```bash
# Snapshot quotidien à chaud (sans interruption de service au bloc)
docker exec generalsurg_db pg_dump -U surguser -d generalsurg_db -F c -b -v -f /tmp/backup_db_$(date +%F).dump
tar -czf /mnt/nfs_hospital/backups/generalsurg_backup_$(date +%F).tar.gz /tmp/backup_db_*.dump /tmp/storage/meshes_v2/
```

---

## 5. Compatibilité des contrats d'API
Les endpoints sous `/api/v2/` visent la stabilité, mais rien ici n'est contractuellement garanti à
long terme — ce dépôt est un prototype, pas un produit versionné avec une politique de support
formelle. Toute extension future doit respecter le schéma relationnel existant (`backend/models.py`,
migrations Alembic) plutôt que de le modifier rétroactivement.
