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

test('affinity target labels survive storage and removal sends the original identifier', async () => {
    const element = () => ({
        style: {}, children: [], textContent: '',
        set innerHTML(value) { this.children = []; },
        appendChild(child) { this.children.push(child); },
    });
    const nodes = new Map();
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, element());
        return nodes.get(id);
    };
    const storage = new Map(), requests = [];
    const context = vm.createContext({
        uploadedAffinityFilesData: [], affinityAggregate: null,
        document: {createElement: element}, $: node,
        fileInfo: element(), removeAllBtnContainer: element(), removeAllBtn: element(),
        removeAllConfirm: element(), affinitySummary: element(), thresholdControl: element(),
        buildMatrixBtn: element(), fileInput: element(),
        sessionStorage: {setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
        fetch: async (url, options) => {
            requests.push({url, body: JSON.parse(options.body)});
            return {ok: true, json: async () => ({all_files: [], num_compounds: 0, num_targets: 0})};
        },
        parseJsonResponse: response => response.json(), resetOptSettingsToDefault() {},
    });
    vm.runInContext(section('function normalizeUploadState', 'const restoredAffinity'), context);
    vm.runInContext(section('function applyUploadResponse', '// Leave headroom'), context);
    vm.runInContext(section('function renderAffinityFiles', '// Build Matrix button'), context);
    context.applyUploadResponse('affinity', {
        all_files: [{name: 'affinity.csv'}], compounds: ['A'], targets: ['P04626', 'CUSTOM'],
        target_labels: {'P04626': 'P04626 -> Receptor kinase (ERBB2)', 'CUSTOM': 'CUSTOM'},
        num_compounds: 1, num_targets: 2, num_datapoints: 2,
    });
    context.renderAffinityFiles();
    let rows = node('#affinityTargetList').children;
    assert.deepEqual(rows.map(row => row.children[0].textContent), ['P04626 -> Receptor kinase (ERBB2)', 'CUSTOM']);
    const restored = context.normalizeUploadState(JSON.parse(storage.get('uploadedAffinityFilesData')));
    context.uploadedAffinityFilesData = restored.files;
    context.affinityAggregate = restored.aggregate;
    context.renderAffinityFiles();
    rows = node('#affinityTargetList').children;
    assert.equal(rows[0].children[0].textContent, 'P04626 -> Receptor kinase (ERBB2)');
    rows[0].children[1].onclick();
    await rows[0].children[1].children[0].onclick();
    assert.deepEqual(requests, [{url: '/api/remove-affinity-target', body: {target: 'P04626'}}]);
});

for (const kind of ['affinity', 'price']) {
    test(`${kind} compound labels survive storage and removal sends the original identifier`, async () => {
        const element = () => ({
            style: {}, children: [], textContent: '',
            set innerHTML(value) { this.children = []; },
            appendChild(child) { this.children.push(child); },
        });
        const nodes = new Map();
        const node = id => {
            if (!nodes.has(id)) nodes.set(id, element());
            return nodes.get(id);
        };
        const storage = new Map(), requests = [];
        const context = vm.createContext({
            uploadedAffinityFilesData: [], affinityAggregate: null,
            uploadedPriceFilesData: [], priceAggregate: null,
            document: {createElement: element}, $: node,
            fileInfo: element(), removeAllBtnContainer: element(), removeAllBtn: element(),
            removeAllConfirm: element(), affinitySummary: element(), thresholdControl: element(),
            buildMatrixBtn: element(), fileInput: element(),
            sessionStorage: {setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
            fetch: async (url, options) => {
                requests.push({url, body: JSON.parse(options.body)});
                return {ok: true, json: async () => ({all_files: [], compounds: [], num_compounds: 0, num_targets: 0, num_prices: 0})};
            },
            parseJsonResponse: response => response.json(), resetOptSettingsToDefault() {},
        });
        vm.runInContext(section('function normalizeUploadState', 'const restoredAffinity'), context);
        vm.runInContext(section('function applyUploadResponse', '// Leave headroom'), context);
        vm.runInContext(section('function renderAffinityFiles', '// Build Matrix button'), context);
        vm.runInContext(section('function renderPriceFiles', '// Remove All buttons'), context);
        const response = {
            all_files: [{name: `${kind}.csv`}], compounds: ['Aspirin', 'CUSTOM'], targets: ['T1', 'T2'],
            compound_labels: {'Aspirin': 'Aspirin -> BSYNRYMUTXBXSQ-UHFFFAOYSA-N (CHEMBL25)'},
            num_compounds: 2, num_targets: 2, num_datapoints: 4, num_prices: 2,
        };
        const render = kind === 'affinity' ? context.renderAffinityFiles : context.renderPriceFiles;
        const list = node(`#${kind}CompoundList`);
        context.applyUploadResponse(kind, response);
        render();
        assert.deepEqual(list.children.map(row => row.children[0].textContent),
                         [response.compound_labels.Aspirin, 'CUSTOM']);

        const storageKey = kind === 'affinity' ? 'uploadedAffinityFilesData' : 'uploadedPriceFilesData';
        const restored = context.normalizeUploadState(JSON.parse(storage.get(storageKey)));
        context[storageKey] = restored.files;
        context[`${kind}Aggregate`] = restored.aggregate;
        render();
        assert.equal(list.children[0].children[0].textContent, response.compound_labels.Aspirin);

        context.applyUploadResponse(kind, {...response, compound_labels: undefined});
        render();
        assert.deepEqual(list.children.map(row => row.children[0].textContent), ['Aspirin', 'CUSTOM']);

        context.applyUploadResponse(kind, response);
        render();
        const row = list.children[0];
        row.children[1].onclick();
        await row.children[1].children[0].onclick();
        assert.deepEqual(requests, [{url: `/api/remove-${kind}-compound`, body: {compound: 'Aspirin'}}]);
    });
}

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
