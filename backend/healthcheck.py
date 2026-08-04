# -*- coding: utf-8 -*-
"""
healthcheck.py — Diagnostic de disponibilité HTTP (pas un contrôle de conformité)
===================================================================================
Script de fumée (smoke test) qui vérifie que quelques endpoints répondent en HTTP 200/201.
Ne teste QUE la disponibilité HTTP des endpoints, jamais leur conformité réglementaire ni
l'exactitude clinique de leurs réponses (plusieurs modules qu'il sonde restent des simulations
explicitement étiquetées comme telles — voir les avertissements dans biomechanics_engine.py et
voice_llm_service.py).
Vérifie uniquement :
    1. La disponibilité de l'API FastAPI et la latence de réponse.
    2. Que le endpoint de statut de conformité répond (sans valider une quelconque conformité réelle).
    3. La disponibilité de l'exemple de gabarit de compte-rendu CCAM (voir voice_llm_service.py —
       appariement de mots-clés, pas un vrai NLP).
"""

import sys
import time
import requests

API_BASE_URL = "http://localhost:8000/api/v2"

def run_healthcheck():
    print("====================================================================")
    print("🏥 GeneralSurgPlan3D — Diagnostic de disponibilité")
    print("====================================================================")

    all_ok = True

    # 1. Disponibilité du endpoint de statut de conformité (PAS une preuve de conformité réelle)
    print("\n[1/3] Vérification de la disponibilité du tableau de bord de statut MDR / FDA...")
    t0 = time.time()
    try:
        resp = requests.get(f"{API_BASE_URL}/compliance/mdr-fda-status", timeout=5)
        lat = (time.time() - t0) * 1000
        if resp.status_code == 200:
            data = resp.json()
            eu_status = data["regulatory_certifications"]["eu_mdr_2017_745"]["status"]
            print(f"      ✅ Endpoint disponible ({lat:.1f} ms) — statut déclaré: {eu_status} "
                  f"(honnête : NOT_CERTIFIED tant qu'aucune évaluation réelle n'a été menée)")
            print(f"      Événements d'audit journalisés: {data['cryptographic_audit_trail']['total_logged_events']}")
        else:
            print(f"      ❌ ERREUR HTTP {resp.status_code}: {resp.text}")
            all_ok = False
    except Exception as e:
        print(f"      ⚠️ API injoignable ({e}). Vérifiez que uvicorn est lancé.")
        all_ok = False

    # 2. Disponibilité du modèle cinématique respiratoire simplifié (PAS un solveur FEM validé)
    print("\n[2/3] Test du modèle cinématique respiratoire simplifié...")
    t0 = time.time()
    try:
        resp = requests.get(f"{API_BASE_URL}/biomech/twins/twin-test-01/respiratory-displacement?phase_rad=1.57", timeout=5)
        lat = (time.time() - t0) * 1000
        if resp.status_code == 200:
            data = resp.json()
            print(f"      ✅ Endpoint disponible ({lat:.1f} ms) — déplacement crânio-caudal simulé ΔZ: "
                  f"{data['global_diaphragm_shift_mm']} mm ({data.get('model_type', 'model_type manquant')})")
        else:
            print(f"      ❌ ERREUR HTTP {resp.status_code}")
            all_ok = False
    except Exception as e:
        print(f"      ⚠️ Erreur réseau: {e}")
        all_ok = False

    # 3. Test de l'exemple de gabarit CCAM (appariement de mots-clés, pas un vrai NLP/LLM)
    print("\n[3/3] Test de l'exemple de gabarit de compte-rendu CCAM...")
    t0 = time.time()
    try:
        payload = {
            "patient_id": "PAT-HEALTHCHECK-999",
            "twin_id": "TWIN-999",
            "surgeon_username": "dr.hadj",
            "specialty": "HBP",
            "raw_voice_transcript": "Réalisation d'une hépatectomie droite réglée par laparotomie avec clampage de 18 minutes.",
            "request_fhir_cda": True
        }
        resp = requests.post(f"{API_BASE_URL}/voice/dictate-report", json=payload, timeout=5)
        lat = (time.time() - t0) * 1000
        if resp.status_code == 201:
            data = resp.json()
            ccam = data["ccam_codes_assigned"][0]["code"] if data["ccam_codes_assigned"] else "UNKNOWN"
            print(f"      ✅ Endpoint disponible ({lat:.1f} ms) — brouillon d'exemple généré (méthode: "
                  f"{data.get('generation_method', 'inconnue')}), acte CCAM indicatif: {ccam}")
        else:
            print(f"      ❌ ERREUR HTTP {resp.status_code}")
            all_ok = False
    except Exception as e:
        print(f"      ⚠️ Erreur réseau: {e}")
        all_ok = False

    print("\n====================================================================")
    if all_ok:
        print("🎉 RÉSULTAT : les endpoints sondés répondent. Ceci NE certifie AUCUNE conformité")
        print("   réglementaire ni exactitude clinique — voir les avertissements de chaque module.")
        sys.exit(0)
    else:
        print("⚠️ RÉSULTAT : ANOMALIES DÉTECTÉES. VÉRIFIEZ LES LOGS SERVEUR.")
        sys.exit(1)

if __name__ == "__main__":
    run_healthcheck()
