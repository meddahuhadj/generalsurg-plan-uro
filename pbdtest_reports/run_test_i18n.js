// Test du code RÉEL (extrait tel quel des fichiers assets/app-part*.js) du moteur I18N :
// interpolation {name}, pluriel ICU-lite {count, plural, one{}other{}}, repli anglais sur clé
// manquante, changement de langue (dont RTL pour l'arabe), détection navigateur, couche de
// surcharge (éditeur de traductions) et export/import JSON.
//
// Usage : node run_test_i18n.js
const fs = require('fs');
const path = require('path');

// Le JS a été extrait de l'ancien HTML monolithique vers assets/app-part*.js
// (voir le découpage frontend) : on reconstitue le même contenu combiné en
// concaténant les 3 fichiers dans leur ordre d'exécution d'origine, pour que
// les recherches de marqueurs ci-dessous continuent de fonctionner à l'identique.
const html = ['app-part1.js', 'app-part2.js', 'app-part3.js']
  .map(f => fs.readFileSync(path.join(__dirname, '..', 'assets', f), 'utf8'))
  .join('\n');

function assert(cond, msg) {
  if (!cond) { console.error('❌ ÉCHEC:', msg); process.exitCode = 1; }
  else console.log('✅', msg);
}

// Extraction : de "const I18N_EMBEDDED = " jusqu'au "})();" qui clôt l'IIFE "const I18N = ...".
const startMarker = 'const I18N_EMBEDDED = ';
const start = html.indexOf(startMarker);
if (start === -1) throw new Error('const I18N_EMBEDDED introuvable dans le HTML');
const iifeRegex = /const I18N = \(function\s*\(\)\s*\{/;
const iifeMatch = iifeRegex.exec(html.slice(start));
if (!iifeMatch) throw new Error('const I18N = (function(){ introuvable');
const iifeStart = start + iifeMatch.index;
// Depuis le début de l'IIFE, on cherche le "})();" correspondant en comptant les accolades.
let i = html.indexOf('{', iifeStart);
let depth = 0;
for (; i < html.length; i++) {
  if (html[i] === '{') depth++;
  else if (html[i] === '}') { depth--; if (depth === 0) { i++; break; } }
}
// i pointe juste après l'accolade fermante de la fonction ; il reste "()," à consommer -> "();"
const iifeEnd = html.indexOf(';', i) + 1;
let code = html.slice(start, iifeEnd);
// `const` déclaré dans un eval() direct reste scopé à cet eval (ES6) — on l'expose sur global.
code = code.replace('const I18N = (function(){', 'global.I18N = (function(){');

// ── Mocks minimaux ──
global.localStorage = {
  _d: {},
  getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
  setItem(k, v) { this._d[k] = String(v); },
};
global.window = {};
global.document = {
  documentElement: { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } },
  body: { classList: { toggle() {} } },
  querySelectorAll() { return []; },
};
// navigator.language : Node 21+ expose un `navigator` global en lecture seule (userAgent web
// compat) — on ne peut pas juste réaffecter `global.navigator`, il faut redéfinir la propriété.
Object.defineProperty(global, 'navigator', { value: { language: 'fr-BE' }, configurable: true });

eval(code);
const I18N = global.I18N;

(async () => {
  // ── 1) Traduction de base + repli EN par défaut ──
  assert(I18N.t('nav.plan') === 'Plan', "t('nav.plan') renvoie la valeur anglaise par défaut avant tout setLocale()");

  // ── 2) Changement de langue instantané, y compris RTL pour l'arabe ──
  await I18N.setLocale('fr');
  assert(I18N.t('sidebar.diagnosis') === 'Diagnostic', "setLocale('fr') change bien la langue active");
  assert(document.documentElement.attrs.dir === 'ltr', "le français reste LTR");

  await I18N.setLocale('ar');
  assert(I18N.t('sidebar.diagnosis') === 'التشخيص', "setLocale('ar') traduit correctement en arabe");
  assert(document.documentElement.attrs.dir === 'rtl', "l'arabe bascule bien dir='rtl' sur <html> (RTL)");
  assert(document.documentElement.attrs.lang === 'ar', "l'attribut lang suit la langue active");

  await I18N.setLocale('nl');
  assert(I18N.t('sidebar.diagnosis') === 'Diagnose', "setLocale('nl') traduit correctement en néerlandais");
  assert(document.documentElement.attrs.dir === 'ltr', "le néerlandais reste LTR");

  // ── 3) Interpolation {name} ──
  await I18N.setLocale('en');
  const dicomMsg = I18N.t('dicom.importing', { count: 7 });
  assert(dicomMsg.includes('7'), `interpolation {count} fonctionne (obtenu: "${dicomMsg}")`);

  // ── 4) Pluriel ICU-lite {count, plural, one{}other{}} ──
  const one = I18N.t('staging.auditLogTitle', { count: 1 });
  const many = I18N.t('staging.auditLogTitle', { count: 5 });
  assert(one.includes('1 entry') && !one.includes('entries'), `forme singulier correcte pour count=1 (obtenu: "${one}")`);
  assert(many.includes('5 entries'), `forme pluriel correcte pour count=5 (obtenu: "${many}")`);

  // ── 5) Repli anglais sur clé manquante dans une langue non-EN + détection des clés manquantes ──
  await I18N.setLocale('fr');
  const missingKeyResult = I18N.t('this.key.does.not.exist');
  assert(missingKeyResult === 'this.key.does.not.exist', 'une clé totalement absente renvoie la clé elle-même (pas de crash)');
  assert(I18N.reportMissing().includes('this.key.does.not.exist'), 'la clé manquante est bien journalisée pour I18N.reportMissing()');

  // ── 6) Détection de la langue du navigateur ──
  assert(I18N.detectBrowserLocale() === 'fr', "detectBrowserLocale() reconnaît 'fr-BE' -> 'fr'");

  // ── 7) Couche de surcharge (éditeur de traductions) — prioritaire sur le dictionnaire de base ──
  await I18N.setLocale('en');
  I18N.setOverride('en', 'nav.plan', 'CUSTOM-OVERRIDDEN-VALUE');
  assert(I18N.t('nav.plan') === 'CUSTOM-OVERRIDDEN-VALUE', "I18N.setOverride() prend le pas sur la valeur de base sans modifier i18n/*.json");

  // ── 8) Export inclut la surcharge fusionnée ; import recharge une couche de surcharge ──
  const exported = await I18N.exportLocale('en');
  assert(exported.nav.plan === 'CUSTOM-OVERRIDDEN-VALUE', "exportLocale() inclut les valeurs surchargées, prêtes à remplacer i18n/en.json");

  I18N.clearOverrides();
  assert(I18N.t('nav.plan') === 'Plan', 'clearOverrides() restaure la valeur de base');

  const importedCount = I18N.importLocale('nl', { nav: { plan: 'GeïmporteerdPlan' } });
  assert(importedCount === 1, 'importLocale() aplatit correctement un objet imbriqué (1 clé importée)');
  await I18N.setLocale('nl');
  assert(I18N.t('nav.plan') === 'GeïmporteerdPlan', 'la clé importée est bien appliquée après setLocale()');
  I18N.clearOverrides();

  // ── 9) Formatage de date/nombre locale-aware (Intl) ──
  await I18N.setLocale('fr');
  const num = I18N.formatNumber(1234.5);
  assert(typeof num === 'string' && num.length > 0, `formatNumber() produit une chaîne locale-aware (obtenu: "${num}")`);

  // ── 10) Les 4 langues déclarées supportées correspondent exactement aux 4 demandées ──
  assert(JSON.stringify(I18N.SUPPORTED.slice().sort()) === JSON.stringify(['ar','en','fr','nl']),
    'I18N.SUPPORTED contient exactement en/fr/ar/nl (support de langues futures documenté, pas codé en dur ailleurs)');

  console.log('\nTerminé.');
})();
