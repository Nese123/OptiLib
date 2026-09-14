const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '../webapp/static/public.js'), 'utf8');

function setup(fetch) {
    const nodes = new Map();
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, {value: 0, style: {}});
        return nodes.get(id);
    };
    const calls = [];
    const storage = new Map();
    const context = vm.createContext({
        window: {fetch, location: {reload() { calls.push('reload'); }}},
        document: {querySelector: selector => ({content: selector.includes('csrf') ? 'token' : 'session'}),
            querySelectorAll: () => [], addEventListener() {}, getElementById: id => ["affinityCompoundList", "priceCompoundList"].includes(id) ? null : node(id)},
        sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key,value), clear: () => storage.clear()},
        Plotly: {newPlot: async (...args) => calls.push(['plot', ...args]), restyle: async (...args) => calls.push(['restyle', ...args]), relayout: async () => {}},
        Response, Headers, DOMException, console, setTimeout: fn => { queueMicrotask(fn); },
        renderAffinityFiles: () => {}, renderPriceFiles: () => {}, loadComparison: async () => {}, loadHeatmap: async () => {}, loadDistributionChart: async () => {},
    });
    vm.runInContext(source, context);
    return {context, calls, node};
}

const tile = (revision, offset = 0) => ({paged: true, revision, matrix: [[1]], compounds: ['A'], targets: ['T'],
    row_offset: offset, column_offset: 0, total_rows: 100000, total_columns: 1000, zmin: -1, zmax: 5, distribution: []});

test('uploads wait for their worker result and propagate job errors', async () => {
    let request = 0;
    const app = setup(async () => {
        request++;
        return request === 1 ? Response.json({job_id: 'job', status_url: '/api/jobs/job'}, {status: 202}) :
            Response.json({status: 'complete', result: {num_prices: 5}});
    });
    const response = await app.context.window.fetch('/api/upload-prices', {method: 'POST'});
    assert.equal(response.status, 200);
    assert.equal((await response.json()).num_prices, 5);
    assert.equal(request, 2);
});

test('heatmap requests bounded windows and discards old selection responses', async () => {
    let resolve;
    const app = setup(async url => {
        assert.match(url, /row_offset=100&column_offset=0/);
        return new Promise(done => { resolve = done; });
    });
    await app.context.loadHeatmap(tile('first'));
    app.node('heatmapCompoundRangeSlider').value = 100;
    const pending = app.node('heatmapCompoundRangeSlider').oninput();
    await app.context.loadHeatmap(tile('second'));
    resolve(Response.json(tile('first', 100)));
    await pending;
    assert.equal(app.calls.filter(call => call[0] === 'restyle').length, 0);
    assert.equal(app.node('heatmapCompoundRangeSlider').max, 99980);
});

test('preparing an export sends CSRF even though completed downloads use GET', async () => {
    const app = setup(async (url, options) => {
        assert.equal(options.headers.get('X-CSRFToken'), 'token');
        return new Response('workbook');
    });
    await app.context.window.fetch('/api/download/library');
});
