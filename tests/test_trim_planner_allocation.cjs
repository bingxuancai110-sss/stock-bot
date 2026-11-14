const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'trim_planner.html'), 'utf8');
const scriptStart = html.search(/<script>\r?\n/) + '<script>'.length;
const scriptEnd = html.indexOf('</script>', scriptStart);
assert.ok(scriptStart > '<script>'.length && scriptEnd > scriptStart);
new vm.Script(html.slice(scriptStart, scriptEnd));
const start = html.indexOf('function allocateDistributedSell(');
const end = html.indexOf('function calc(){', start);
assert.ok(start >= 0 && end > start, 'calculator allocation function exists');
const allocate = vm.runInNewContext(`${html.slice(start, end)}; allocateDistributedSell`);
const allocateByMode = vm.runInNewContext(`${html.slice(start, end)}; allocateSellByMode`);

const positions = [
  { index: 0, code: 'WEAK', value: 400000 },
  { index: 1, code: 'MIXED', value: 350000 },
  { index: 2, code: 'STRONG', value: 250000 },
];
const ranking = {
  WEAK: { category: 'weak' },
  MIXED: { category: 'mixed' },
  STRONG: { category: 'strong' },
};
function close(actual, expected) {
  assert.ok(Math.abs(actual - expected) < 0.01, `${actual} differs from ${expected}`);
}

const tilted = allocate(positions, 300000, 0.5, ranking, true);
close(tilted.amounts.reduce((sum, amount) => sum + amount, 0), 300000);
close(tilted.remaining, 0);
assert.ok(tilted.amounts[0] / positions[0].value > tilted.amounts[1] / positions[1].value);
assert.ok(tilted.amounts[1] / positions[1].value > tilted.amounts[2] / positions[2].value);
assert.ok(tilted.amounts.every((amount, index) => amount < positions[index].value * 0.5));

const proportional = allocate(positions, 300000, 0.5, ranking, false);
proportional.amounts.forEach((amount, index) => close(amount, positions[index].value * 0.3));
close(proportional.remaining, 0);

const atCap = allocate(positions, 500000, 0.5, ranking, true);
atCap.amounts.forEach((amount, index) => close(amount, positions[index].value * 0.5));
close(atCap.remaining, 0);

const overCap = allocate(positions, 600000, 0.5, ranking, true);
close(overCap.amounts.reduce((sum, amount) => sum + amount, 0), 500000);
close(overCap.remaining, 100000);

for (const requested of [600000, 700000]) {
  for (const tilted of [false, true]) {
    const result = allocate(positions, requested, 1, ranking, tilted);
    close(result.amounts.reduce((sum, amount) => sum + amount, 0), requested);
    close(result.remaining, 0);
    assert.ok(result.amounts.every((amount, index) => amount <= positions[index].value));
  }
}

assert.match(html, /id="perStockCap"[^>]*value="50"/);
assert.match(html, /id="trimAmountPct"[^>]*value="20"/);
const trimStart = html.indexOf('function recommendedTrimForMode(');
const trimEnd = html.indexOf('function syncRecommendedTrim(){', trimStart);
const recommendedTrim = vm.runInNewContext(`${html.slice(trimStart, trimEnd)}; recommendedTrimForMode`);
assert.equal(recommendedTrim('largest'), 20);
assert.equal(recommendedTrim('weakfirst'), 30);
assert.equal(recommendedTrim('proportional'), 30);
const capStart = html.indexOf('function recommendedPerStockCap(');
const capEnd = html.indexOf('function syncRecommendedCap(){', capStart);
const recommendedCap = vm.runInNewContext(`${html.slice(capStart, capEnd)}; recommendedPerStockCap`, {
  clamp: value => Math.max(0, Math.min(100, value)),
});
assert.equal(recommendedCap(100, 40), 50);
assert.equal(recommendedCap(100, 60), 70);
assert.equal(recommendedCap(100, 70), 80);
assert.equal(recommendedCap(85, 40), 58);
for (const requested of [400000, 600000, 700000]) {
  const cap = recommendedCap(100, requested / 10000) / 100;
  const result = allocateByMode(positions, requested, cap, ranking, 'largest');
  close(result.remaining, 0);
  assert.ok(result.amounts.every((amount, index) => amount < positions[index].value));
}

const modes = ['largest', 'weakfirst', 'proportional'].map(mode =>
  allocateByMode(positions, 400000, 1, ranking, mode).amounts,
);
modes.forEach(amounts => close(amounts.reduce((sum, amount) => sum + amount, 0), 400000));
assert.ok(modes[0].some((amount, index) => Math.abs(amount - modes[1][index]) > 1));
assert.ok(modes[1].some((amount, index) => Math.abs(amount - modes[2][index]) > 1));
const forced = ['largest', 'weakfirst', 'proportional'].map(mode =>
  allocateByMode(positions, 500000, 0.5, ranking, mode).amounts,
);
forced.forEach(amounts => amounts.forEach((amount, index) => close(amount, positions[index].value * 0.5)));

const elements = Object.fromEntries(['methodCompareNote', 'methodCompareOptions', 'methodCompareStocks'].map(id => [id, {}]));
const renderComparison = vm.runInNewContext(`${html.slice(start, end)}; renderMethodComparison`, {
  el: id => elements[id],
  money: value => `$${Math.round(value)}`,
  pct: value => `${value.toFixed(1)}%`,
  esc: value => value,
  recommendedTrimForMode: recommendedTrim,
  trimFollowsMode: false,
});
renderComparison(positions, 400000, 1, ranking, 'weakfirst', 1000000);
assert.match(elements.methodCompareNote.textContent, /每檔賣出金額/);
assert.match(elements.methodCompareOptions.innerHTML, /data-pick-allocation="largest"/);
assert.match(elements.methodCompareOptions.innerHTML, /data-pick-allocation="weakfirst"/);
assert.match(elements.methodCompareOptions.innerHTML, /data-pick-allocation="proportional"/);
assert.match(elements.methodCompareStocks.innerHTML, /高力|WEAK/);
renderComparison(positions, 500000, 0.5, ranking, 'weakfirst', 1000000);
assert.match(elements.methodCompareNote.textContent, /三種方式真的會一樣/);

console.log('Trim allocation scenarios passed');
