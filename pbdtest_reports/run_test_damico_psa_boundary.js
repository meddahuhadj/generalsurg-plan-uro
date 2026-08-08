// Test du code RÉEL (extrait de assets/app-part2.js) pour la stratification de risque de
// D'Amico (cancer de prostate localisé), calculée dans updateStagingDecision().
//
// Bug corrigé : le critère PSA du risque "faible" utilisait `psa < 10` (strict) au lieu de
// `psa <= 10` (borne incluse) — déviation du critère original D'Amico 1998 ("PSA <= 10 ng/mL").
// Un patient avec un PSA exactement à 10.0 ng/mL (cas réel, pas un cas d'école) était donc
// classé "intermédiaire" au lieu de "faible", ce qui peut orienter vers une prostatectomie
// radicale/radiothérapie alors qu'une surveillance active serait cliniquement recommandée.
//
// Usage : node run_test_damico_psa_boundary.js
const fs = require('fs');
const path = require('path');

const html = ['app-part1.js', 'app-part2.js', 'app-part3.js']
  .map(f => fs.readFileSync(path.join(__dirname, '..', 'assets', f), 'utf8'))
  .join('\n');

function assert(cond, msg) {
  if (!cond) { console.error('❌ ÉCHEC:', msg); process.exitCode = 1; }
  else console.log('✅', msg);
}

// ── Extraction de updateStagingDecision() telle quelle (déclaration de fonction, pas un littéral) ──
function extractFunction(src, name) {
  const marker = `function ${name}() {`;
  const start = src.indexOf(marker);
  if (start === -1) throw new Error(`Fonction introuvable dans le source : ${name}`);
  let i = src.indexOf('{', start);
  let depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) { i++; break; } }
  }
  return src.slice(start, i);
}
const code = extractFunction(html, 'updateStagingDecision')
  .replace('function updateStagingDecision() {', 'global.updateStagingDecision = function () {');
eval(code);

// ── Mocks minimaux : DOM (valeurs des champs de staging), state, I18N, dépendances externes ──
function runStaging({ psa, gleason, pStage }) {
  const fieldValues = {
    'stg-T': 'T2', 'stg-N': 'N0', 'stg-M': 'M0',
    'stg-bclc': '', 'stg-child': '', 'stg-crm': '', 'stg-vems': '',
    'stg-renal': '', 'stg-kidney-side': '', 'stg-pirads': '',
    'stg-gleason': gleason, 'stg-dfg': '', 'stg-bosniak': '',
    'stg-psa': String(psa), 'stg-pstage': pStage,
  };
  const box = { innerHTML: '' }; // même instance à chaque getElementById() — pas un objet neuf
  global.document = {
    getElementById(id) {
      if (id in fieldValues) return { value: fieldValues[id] };
      if (id === 'staging-decision-box') return box;
      return null;
    },
  };
  global.state = { mpr: { _stagingData: {} }, mod: 'urologie' };
  global.I18N = { t: (key) => key };
  global.logAudit = () => {};
  global.fetchRealRenalNephrometry = () => {};

  global.updateStagingDecision();
  return box.innerHTML;
}

// ── PSA exactement à 10.0 ng/mL, Gleason 3+3 (ISUP 1), stade cT1c -> devrait être "faible" ──
const htmlAt10 = runStaging({ psa: 10.0, gleason: 'Gleason 3+3 (ISUP 1)', pStage: 'cT1c' });
assert(htmlAt10.includes("Risque de D'Amico faible"),
  "PSA=10.0 (borne incluse), ISUP 1, cT1c -> risque FAIBLE (critère D'Amico original : PSA <= 10)");
assert(!htmlAt10.includes("Risque de D'Amico intermédiaire"),
  "PSA=10.0 ne doit plus être classé à tort en risque intermédiaire");

// ── PSA juste au-dessus (10.1) -> intermédiaire (comportement inchangé, contrôle de non-régression) ──
const htmlAt10_1 = runStaging({ psa: 10.1, gleason: 'Gleason 3+3 (ISUP 1)', pStage: 'cT1c' });
assert(htmlAt10_1.includes("Risque de D'Amico intermédiaire"),
  "PSA=10.1 (juste au-dessus de la borne) reste bien classé intermédiaire");

// ── PSA nettement bas (5.0) -> faible (contrôle de non-régression, cas déjà correct avant le correctif) ──
const htmlAt5 = runStaging({ psa: 5.0, gleason: 'Gleason 3+3 (ISUP 1)', pStage: 'cT1c' });
assert(htmlAt5.includes("Risque de D'Amico faible"), "PSA=5.0, ISUP 1, cT1c reste classé faible");

console.log('\nTerminé.');
