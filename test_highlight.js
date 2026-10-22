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
// Comment markdown: GitHub's block and inline forms, escaped, with links only to http(s)/mailto.
eval(src.match(/const MD_FENCE[\s\S]*?\nconst MD_TABLE_SEP[^\n]*\n/)[0].replace(/const /g, 'var ') + grab('md') + grab('mdCode') + grab('mdInline'));
assert.equal(md('Hi `a<b>` and **bold**, *it*, _it_, ~~gone~~'),
  '<p>Hi <code>a&lt;b&gt;</code> and <strong>bold</strong>, <em>it</em>, <em>it</em>, <del>gone</del></p>');
assert.equal(md('snake_case_name and 2*3*4'), '<p>snake_case_name and 2*3*4</p>');
assert.equal(md('line one\nline two\n\nnext'), '<p>line one<br>line two</p><p>next</p>');
assert.equal(md('Text:\n\n    let x = 1\n    <y>\n\nafter'), '<p>Text:</p><pre><code>let x = 1\n&lt;y&gt;</code></pre><p>after</p>');
const fenced = md('```swift\nlet s = "<x>"\n```');
assert.ok(fenced.startsWith('<pre><code>') && fenced.includes('hljs-keyword') && fenced.includes('&lt;x&gt;'), fenced);
assert.equal(md('~~~\n**not bold**\n~~~'), '<pre><code>**not bold**</code></pre>');
assert.equal(md('- one\n- two `x`\n  continued\n- three'), '<ul><li><p>one</p></li><li><p>two <code>x</code><br>continued</p></li><li><p>three</p></li></ul>');
assert.equal(md('1. a\n2. b\n   - nested\n3. c'), '<ol><li><p>a</p></li><li><p>b</p><ul><li><p>nested</p></li></ul></li><li><p>c</p></li></ol>');
assert.equal(md('3) x'), '<ol start="3"><li><p>x</p></li></ol>');
assert.equal(md('- item\n\n      code in item\n- next'), '<ul><li><p>item</p><pre><code>code in item</code></pre></li><li><p>next</p></li></ul>');
assert.equal(md('## Title #\n> quoted *x*\n> more\n\n---'), '<h2>Title</h2><blockquote><p>quoted <em>x</em><br>more</p></blockquote><hr>');
assert.equal(md('| a | b |\n|:--|--:|\n| `1\\|x` | **2** |'),
  '<table><thead><tr><th style="text-align:left">a</th><th style="text-align:right">b</th></tr></thead>' +
  '<tbody><tr><td style="text-align:left"><code>1|x</code></td><td style="text-align:right"><strong>2</strong></td></tr></tbody></table>');
assert.equal(md('[docs](https://x.dev/a?b=1&c=2) and https://y.dev/p.'),
  '<p><a href="https://x.dev/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">docs</a> and ' +
  '<a href="https://y.dev/p" target="_blank" rel="noopener noreferrer">https://y.dev/p</a>.</p>');
assert.equal(md('[x](javascript:alert(1)) <img src=x onerror=alert(1)>'),
  '<p>[x](javascript:alert(1)) &lt;img src=x onerror=alert(1)&gt;</p>');
assert.equal(md('\\*not em\\* `` a`b ``'), '<p>*not em* <code>a`b</code></p>');
assert.ok(!md('[a](https://x.dev/"onmouseover="alert(1))').includes('"onmouseover'));
// Unlabelled code takes the discussed file's language; a fence's own label wins.
const swiftIndented = md('    let s = "x"', 'swift');
assert.ok(swiftIndented.includes('hljs-keyword') && swiftIndented.includes('hljs-string'), swiftIndented);
assert.ok(md('- a\n\n      let x = 1', 'swift').includes('hljs-keyword'));
assert.ok(md('```\nval x = 1\n```', 'kotlin').includes('hljs-keyword'));
assert.ok(!md('```text\nlet x = 1\n```', 'swift').includes('hljs-'));
assert.ok(!md('    let x = 1').includes('hljs-'));
console.log('highlight tests OK'); console.log(out[1]);
