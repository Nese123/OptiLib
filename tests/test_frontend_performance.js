// Run with: node --test tests/test_frontend_performance.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../webapp/static/app.js'), 'utf8');
function section(start, end) { return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start))); }

test('upload batches preserve order and bound both byte and file counts', () => {
    const context = vm.createContext({});
    vm.runInContext(section('function* uploadBatches', 'let pipelinePollTimer'), context);
    const files = Array.from({length: 53}, (_, i) => ({name: `${i}.csv`, size: 2}));
    const batches = Array.from(context.uploadBatches(files, 15, 5));
    assert.deepEqual(batches.flat(), files);
    assert.ok(batches.every(batch => batch.length <= 5 && batch.reduce((n, f) => n + f.size, 0) <= 15));
    assert.throws(() => Array.from(context.uploadBatches([{name: 'large.csv', size: 20}], 15)), /exceeds/);
});

test('legacy upload aggregates are migrated once beside compact file entries', () => {
    const context = vm.createContext({});
    vm.runInContext(section('function normalizeUploadState', 'const restoredAffinity'), context);
    const legacy = [{name: 'one', data: {num_prices: 2}, allCompounds: ['A', 'B'], totalUnique: 2},
                    {name: 'two', data: {num_prices: 1}, allCompounds: ['A', 'B'], totalUnique: 2}];
    const migrated = context.normalizeUploadState(legacy);
    assert.deepEqual(JSON.parse(JSON.stringify(migrated.files)),
                     [{name: 'one', data: {num_prices: 2}}, {name: 'two', data: {num_prices: 1}}]);
    assert.deepEqual(Array.from(migrated.aggregate.allCompounds), ['A', 'B']);
    assert.equal(migrated.aggregate.data, undefined);
    assert.equal(context.normalizeUploadState(migrated), migrated);
});

test('heatmap creates only a viewport of values and hover metadata', () => {
    const context = vm.createContext({});
    vm.runInContext(section('function heatmapWindow', 'async function loadHeatmap'), context);
    const data = {targets: Array.from({length: 80}, (_, i) => `T${i}`),
                  compounds: Array.from({length: 100}, (_, i) => `C${i}`),
                  matrix: Array.from({length: 100}, (_, i) => Array.from({length: 80}, (_, j) => i * 80 + j))};
    data.matrix[40][30] = null;
    const result = context.heatmapWindow(data, data.targets, data.compounds, 30, 40);
    assert.equal(result.z.length, 20);
    assert.equal(result.z[0].length, 40);
    assert.equal(result.customdata.flat().length, 800);
    assert.equal(result.text[0][0], 'No Data');
    assert.equal(result.z[19][39], data.matrix[59][69]);
    assert.deepEqual(Array.from(result.customdata[19][39]), ['C59', 'T69']);
    const edge = context.heatmapWindow(data, data.targets, data.compounds, 79, 99);
    assert.equal(edge.z.length, 1);
    assert.equal(edge.z[0].length, 1);
});

test('polling is single flight and ignores a response after cancellation', async () => {
    const timers = new Map(); let next = 0, resolveResponse, calls = 0, published = 0;
    const context = vm.createContext({AbortController,
        setTimeout: fn => { timers.set(++next, fn); return next; },
        clearTimeout: id => timers.delete(id),
        fetch: () => { calls++; return new Promise(resolve => { resolveResponse = resolve; }); },
    });
    vm.runInContext(section('function createPoller', 'function startPipelinePolling'), context);
    const poller = context.createPoller('/status', 500, () => { published++; });
    const first = timers.values().next().value; timers.clear();
    const inFlight = first();
    assert.equal(calls, 1);
    assert.equal(timers.size, 0);
    resolveResponse({ok: true, json: async () => ({generation: 1})});
    await inFlight;
    assert.equal(published, 1);
    assert.equal(timers.size, 1);
    const second = timers.values().next().value; timers.clear();
    const pending = second();
    poller.cancel();
    resolveResponse({ok: true, json: async () => ({generation: 2})});
    await pending;
    assert.equal(published, 1);
    assert.equal(timers.size, 0);
});
