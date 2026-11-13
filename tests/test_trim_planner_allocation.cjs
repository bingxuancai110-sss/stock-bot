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

console.log('Trim allocation scenarios passed');
