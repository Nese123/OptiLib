// Run with: node tests/test_frontend_admission.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../webapp/static/app.js'), 'utf8');

function section(start, end) {
    const startIndex = source.indexOf(start);
    const endIndex = source.indexOf(end, startIndex + start.length);
    assert.ok(startIndex >= 0 && endIndex > startIndex);
    return source.slice(startIndex, endIndex);
}

function setup(mode = 'target') {
    const nodes = new Map();
    function $(selector) {
        if (!nodes.has(selector)) {
            const classes = new Set(['visible']);
            const node = {
                value: '1', checked: false, disabled: false,
                style: { display: '', visibility: 'hidden' },
                classList: { remove: name => classes.delete(name), contains: name => classes.has(name) },
                addEventListener: (event, handler) => { node[event] = handler; },
            };
            nodes.set(selector, node);
        }
        return nodes.get(selector);
    }
    const calls = { requests: [], chart: 0, resetSettings: 0, saveSettings: 0, polling: 0, steps: [] };
    let resolveRequest;
    let rejectRequest;
    const response = new Promise((resolve, reject) => {
        resolveRequest = resolve;
        rejectRequest = reject;
    });
    const context = {
        $, document: { getElementById: id => $(`#${id}`) },
        buildMatrixBtn: $('#buildMatrixBtn'), runOptBtn: $('#runOptBtn'),
        optError: $('#optError'), uploadError: $('#uploadError'),
        optStatusIndicator: $('#optStatusIndicator'),
        selectivityThreshold: $('#selectivityThreshold'),
        weightMean: $('#weightMean'), allowedMiss: $('#allowedMiss'),
        uploadMode: mode, uploadedChemblIds: ['CHEMBL1'], uploadedMatchedCount: 1,
        fetch: (url, options) => { calls.requests.push({ url, options }); return response; },
        parseJsonResponse: result => result.json(),
        showError: (node, message) => { node.textContent = message; node.style.display = 'block'; },
        resetOptSettingsToDefault: () => { calls.resetSettings++; },
        saveOptSettings: () => { calls.saveSettings++; },
        initHistoryChart: () => { calls.chart++; },
        startOptPolling: () => { calls.polling++; },
        startPipelinePolling: () => { calls.polling++; },
        goToStep: step => calls.steps.push(step),
        loadDatasetInfo: async () => {},
    };
    $('#runOptBtn').style.display = 'none';
    $('#runAgainBtn').style.display = 'inline-flex';
    $('#seeResultsBtn').style.display = 'inline-flex';
    $('#stopOptBtn').style.display = 'none';
    vm.createContext(context);
    vm.runInContext(section('// Build Matrix button', '// ═══════════════════════════════════════════════════════════════'), context);
    vm.runInContext(section('// Run Optimization\n', '\nfunction startOptPolling'), context);
    vm.runInContext(section('// Run Again button', '\nasync function resetAllState'), context);
    vm.runInContext(section('// Navigation buttons on step 4', '// New run button'), context);
    // Keep native click dispatch separate from the registered click handler.
    const runHandler = $('#runOptBtn').click;
    $('#runOptBtn').click = () => {
        calls.runPromise = runHandler();
        return calls.runPromise;
    };
    return {
        $, calls,
        admit: () => resolveRequest({ ok: true, json: async () => ({ status: 'started' }) }),
        refuse: () => resolveRequest({ ok: false, json: async () => ({ error: 'Server busy' }) }),
        fail: () => rejectRequest(new Error('Connection failed')),
    };
}

for (const outcome of ['refuse', 'fail', 'admit']) {
    test(`Run Again preserves results until admission: ${outcome}`, async () => {
        const app = setup();
        app.$('#runAgainBtn').click();
        assert.equal(app.calls.requests.length, 1);
        assert.equal(app.calls.requests[0].url, '/api/run');
        assert.equal(app.calls.chart, 0);
        assert.equal(app.calls.saveSettings, 0);
        assert.equal(app.$('#optCompleteBanner').style.display, '');
        assert.equal(app.$('#seeResultsBtn').style.display, 'inline-flex');
        assert.equal(app.$('#stopOptBtn').style.display, 'none');
        app[outcome]();
        await app.calls.runPromise;
        if (outcome === 'admit') {
            assert.equal(app.calls.chart, 1);
            assert.equal(app.calls.saveSettings, 1);
            assert.equal(app.calls.polling, 1);
            assert.equal(app.$('#optCompleteBanner').style.display, 'none');
            assert.equal(app.$('#stopOptBtn').style.display, 'inline-flex');
        } else {
            assert.equal(app.calls.chart, 0);
            assert.equal(app.calls.saveSettings, 0);
            assert.equal(app.calls.polling, 0);
            assert.equal(app.$('#optCompleteBanner').style.display, '');
            assert.equal(app.$('#optCompleteBanner').classList.contains('visible'), true);
            assert.equal(app.$('#runAgainBtn').style.display, 'inline-flex');
            assert.equal(app.$('#runAgainBtn').disabled, false);
            assert.equal(app.$('#seeResultsBtn').style.display, 'inline-flex');
        }
    });
}

for (const mode of ['target', 'affinity']) {
    for (const outcome of ['refuse', 'fail', 'admit']) {
        test(`${mode} matrix build preserves settings until admission: ${outcome}`, async () => {
            const app = setup(mode);
            const pending = app.$('#buildMatrixBtn').click();
            assert.equal(app.calls.resetSettings, 0);
            assert.equal(app.calls.steps.length, 0);
            app[outcome]();
            await pending;
            assert.equal(app.calls.resetSettings, outcome === 'admit' ? 1 : 0);
            assert.equal(app.calls.polling, outcome === 'admit' ? 1 : 0);
            assert.deepEqual(app.calls.steps, outcome === 'admit' ? [2] : []);
            if (outcome !== 'admit') assert.equal(app.$('#buildMatrixBtn').disabled, false);
        });
    }
}

test('Re-configure & Run Again preserves the existing result without resetting the server', async () => {
    const app = setup();
    await app.$('#backToStep3Btn').click();
    assert.equal(app.calls.requests.length, 0);
    assert.equal(app.calls.chart, 0);
    assert.equal(app.calls.resetSettings, 0);
    assert.equal(app.$('#optCompleteBanner').style.display, '');
    assert.equal(app.$('#seeResultsBtn').style.display, 'inline-flex');
    assert.deepEqual(app.calls.steps, [3]);
});
