// Tests syntax-highlight language detection and line splitting. Run: node test_highlight.js
const fs = require('fs');
const hljs = require(__dirname + '/vendor/highlight.min.js');
global.hljs = hljs; for (const l of ['groovy','gradle','properties','protobuf','dart']) (0, eval)(fs.readFileSync(__dirname + '/vendor/hljs-' + l + '.min.js', 'utf8'));
const src = fs.readFileSync(__dirname + '/ui.html', 'utf8').match(/<script>([\s\S]*?)<\/script>/g).pop().replace(/<\/?script>/g, '');
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const grab = name => src.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n}\\n`))[0];
eval(src.match(/const LANG_ALIASES[^\n]*\n/)[0].replace('const', 'var') + grab('langFor') + grab('highlightLines'));
const assert = require('assert');
assert.equal(langFor('a/B.kt'), 'kt'); assert.equal(langFor('build.gradle.kts'), 'kts'); assert.equal(langFor('x.gradle'), 'gradle');
assert.equal(langFor('res/values/strings.xml'), 'xml'); assert.equal(langFor('gradle.properties'), 'properties');
assert.equal(langFor('libs.versions.toml'), 'ini'); assert.equal(langFor('LICENSE'), null); assert.equal(langFor('x.weird'), null);
const kt = ['/**', ' * multi-line <doc>', ' */', 'val x = "a<b" // c', 'fun f(): Int = 1'];
const out = highlightLines(kt, 'kotlin');
assert.equal(out.length, 5);
out.forEach(l => { assert.equal((l.match(/<span/g)||[]).length, (l.match(/<\/span>/g)||[]).length, l); });
assert.ok(out[1].includes('hljs-comment') && out[1].includes('&lt;doc&gt;'), out[1]);
assert.ok(out[3].includes('hljs-string') && out[3].includes('&lt;'), out[3]);
const strip = h => h.replace(/<[^>]+>/g, '').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/&amp;/g,'&').replace(/&#x27;|&#39;/g,"'");
out.forEach((l, i) => assert.equal(strip(l), kt[i]));
const xml = highlightLines(['<string name="a">Hi &amp; bye</string>', ''], 'xml');
assert.equal(xml.length, 2); assert.equal(strip(xml[0]), '<string name="a">Hi &amp; bye</string>');
assert.deepEqual(highlightLines(['<x>'], null), ['&lt;x&gt;']);
// Side by side: context fills both halves, a change pairs dels with adds row by row, leftovers go blank.
eval(grab('fillSideBySide'));
const el = {};
fillSideBySide({old_path: 'a.txt', new_path: 'a.txt', hunks: [{header: '@@ -1,4 +1,3 @@', lines: [
  {t: 'ctx', o: 1, n: 1, s: 'same'}, {t: 'del', o: 2, n: null, s: 'old1'}, {t: 'del', o: 3, n: null, s: 'old2'},
  {t: 'add', o: null, n: 2, s: 'new1'}, {t: 'ctx', o: 4, n: 3, s: 'tail'}]}]}, el);
const sbsRows = [...el.innerHTML.matchAll(/<tr class="ln (\w+)"[^>]*data-o="(\d*)" data-n="(\d*)">/g)].map(m => m.slice(1).join(':'));
assert.deepEqual(sbsRows, ['ctx:1:1', 'change:2:2', 'change:3:', 'ctx:4:3']);
assert.equal((el.innerHTML.match(/class="code blank"/g) || []).length, 1);
assert.ok(el.innerHTML.includes('data-side="old"') && el.innerHTML.includes('colspan="4"'));
console.log('highlight tests OK'); console.log(out[1]);
