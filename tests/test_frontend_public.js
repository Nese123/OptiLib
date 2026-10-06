const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../webapp/static/public.js'), 'utf8');
const appSource = fs.readFileSync(path.join(__dirname, '../webapp/static/app.js'), 'utf8');
const heatmapSource = appSource.slice(appSource.indexOf('function heatmapWindow('),
    appSource.indexOf('async function loadDistributionChart('));
const plain = value => JSON.parse(JSON.stringify(value));

function setup(fetch, {publicService = true, newPlotReady = async () => {}, updateReady = async () => {},
    preloadFetch = () => new Promise(() => {})} = {}) {
    const nodes = new Map();
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, {value: 0, style: {}, clientHeight: 390, on() {}});
        return nodes.get(id);
    };
    const calls = [];
    const requests = [];
    const listeners = new Map();
    const delays = [];
    const errors = [];
    const plots = new Map();
    const frames = new Map();
    let nextFrame = 0;
    const observers = [];
    class ResizeObserver {
        constructor(callback) {
            this.callback = callback;
            this.nodes = [];
            observers.push(this);
        }
        observe(element) { this.nodes.push(element); }
    }
    const storage = new Map();
    const apiFetch = (url, options = {}) => {
        requests.push({url, options});
        return String(url).includes('preload=1') ? preloadFetch(url, options) : fetch(url, options);
    };
    const context = vm.createContext({
        window: {fetch: apiFetch, ResizeObserver, location: {href: 'http://localhost/optimize',
            reload() { calls.push('reload'); }, assign(url) { calls.push(['navigate', url]); }}},
        document: {querySelector: selector => selector.startsWith('#') ? node(selector.slice(1)) :
            {content: selector.includes('csrf') ? 'token' : 'session'},
            querySelectorAll: () => [], addEventListener: (event, callback) => listeners.set(event, callback), getElementById: node},
        sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key,value), clear: () => storage.clear()},
        Plotly: {
            newPlot: async (id, traces, layout, config) => {
                calls.push(['plot', id, traces, layout, config]);
                plots.set(id, plain({traces, layout, config}));
                await newPlotReady();
            },
            update: async (id, traceUpdates, layoutUpdates) => {
                calls.push(['update', id, traceUpdates, layoutUpdates]);
                const plot = plots.get(id);
                for (const [key, values] of Object.entries(traceUpdates)) plot.traces[0][key] = plain(values[0]);
                for (const [key, value] of Object.entries(layoutUpdates)) {
                    const [axis, property] = key.split('.');
                    plot.layout[axis][property] = plain(value);
                }
                await updateReady();
            },
            Plots: {resize: id => calls.push(['resize', id])},
        },
        Response, Headers, DOMException, ResizeObserver, AbortController, URL,
        console: {...console, error: (...args) => errors.push(args)},
        requestAnimationFrame: callback => { frames.set(++nextFrame, callback); return nextFrame; },
        cancelAnimationFrame: id => frames.delete(id),
        setTimeout: (callback, delay) => { delays.push(delay); queueMicrotask(callback); },
        loadComparison: async () => {},
    });
    context.$ = selector => context.document.querySelector(selector);
    Object.defineProperty(context, 'fetch', {get: () => context.window.fetch});
    vm.runInContext(heatmapSource, context);
    if (publicService) vm.runInContext(source, context);
    const flushFrames = () => {
        const callbacks = [...frames.values()];
        frames.clear();
        return Promise.all(callbacks.map(callback => callback()));
    };
    const input = (id, value) => {
        const slider = node(id);
        slider.value = value;
        slider.oninput({target: slider});
    };
    const click = link => listeners.get('click')({target: {closest: () => link}, preventDefault() {}});
    return {context, calls, requests, errors, node, plots, observers, flushFrames, input, delays, click};
}

function downloadLink(href = '/api/download/library') {
    const attributes = new Map([['href', href]]);
    return {
        childNodes: [{icon: 'download'}, {text: 'Optimized Library (.xlsx)'}],
        getAttribute: name => attributes.get(name) ?? null,
        setAttribute: (name, value) => attributes.set(name, value),
        removeAttribute: name => attributes.delete(name),
        replaceChildren(...children) { this.childNodes = children; },
    };
}

function dataset(rows = 47, columns = 55) {
    const matrix = Array.from({length: rows}, (_, row) => Array.from({length: columns}, (_, column) =>
        (row + column) % 11 === 0 ? null : (row * columns + column) % 13 - 6));
    matrix[0][columns - 1] = -8;
    matrix[rows - 1][columns - 1] = 9;
    return {matrix, compounds: Array.from({length: rows}, (_, row) => `CHEMBL${row + 1}`),
        targets: Array.from({length: columns}, (_, column) => `T${column + 1}`),
        target_names: Array.from({length: columns}, (_, column) => `Protein target ${column + 1}`)};
}

function tile(data, rowOffset = 0, columnOffset = 0, overrides = {}, rowCount = 20, columnCount = 40) {
    return {paged: true, revision: 'current',
        matrix: data.matrix.slice(rowOffset, rowOffset + rowCount).map(row => row.slice(columnOffset, columnOffset + columnCount)),
        compounds: data.compounds.slice(rowOffset, rowOffset + rowCount),
        targets: data.targets.slice(columnOffset, columnOffset + columnCount),
        target_names: data.target_names.slice(columnOffset, columnOffset + columnCount),
        row_offset: rowOffset, column_offset: columnOffset,
        total_rows: data.compounds.length, total_columns: data.targets.length,
        zmin: -8, zmax: 9, distribution: [], ...overrides};
}

const updates = app => app.calls.filter(call => call[0] === 'update');
const settleBackground = () => new Promise(setImmediate);
const preload = (data, revision = 'current') => ({...data, paged: false, revision, zmin: -8, zmax: 9});
function windowResponse(data, url, overrides = {}) {
    const params = new URL(url, 'http://localhost').searchParams;
    return Response.json(tile(data, Number(params.get('row_offset')), Number(params.get('column_offset')),
        overrides, Number(params.get('row_count') || 20), Number(params.get('column_count') || 40)));
}

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

    const failed = setup(async url => url === '/api/upload-prices' ?
        Response.json({status_url: '/api/jobs/job'}, {status: 202}) :
        Response.json({status: 'error', error: 'Invalid price data'}));
    const errorResponse = await failed.context.window.fetch('/api/upload-prices', {method: 'POST'});
    assert.equal(errorResponse.status, 400);
    assert.equal((await errorResponse.json()).error, 'Invalid price data');
});

test('paged and local heatmaps share labels, layout, color scale and responsive sizing', async () => {
    const data = dataset();
    const unexpectedFetch = async url => assert.fail(`Unexpected request: ${url}`);
    const local = setup(unexpectedFetch, {publicService: false});
    const publicApp = setup(unexpectedFetch);
    await local.context.loadHeatmap(data);
    await publicApp.context.loadHeatmap(tile(data));
    await Promise.all([local.flushFrames(), publicApp.flushFrames()]);

    assert.deepEqual(publicApp.plots.get('heatmapChart'), local.plots.get('heatmapChart'));
    const {traces: [trace], layout, config} = publicApp.plots.get('heatmapChart');
    assert.equal(trace.type, 'heatmap');
    assert.equal(trace.colorscale, 'Viridis');
    assert.equal(trace.showscale, true);
    assert.equal(trace.zauto, false);
    assert.deepEqual([trace.zmin, trace.zmax], [-8, 9]);
    assert.equal(trace.colorbar.thickness, 18);
    assert.equal(layout.plot_bgcolor, 'rgba(255,255,255,0.08)');
    assert.equal(layout.xaxis.showgrid, false);
    assert.equal(layout.yaxis.showgrid, false);
    assert.equal(layout.xaxis.side, 'top');
    assert.equal(layout.xaxis.tickangle, -90);
    assert.equal(layout.font.size, 10);
    assert.deepEqual(layout.margin, {l: 95, r: 95, t: 65, b: 25});
    assert.equal(layout.xaxis.ticktext[0], 'T1');
    assert.deepEqual(trace.customdata[0][0], ['CHEMBL1', 'Protein target 1 (T1)']);
    assert.equal(trace.text[0][0], 'No Data');
    assert.equal(trace.text[0][1], '-5.00');
    assert.equal(config.responsive, true);
    assert.equal(config.displayModeBar, false);
    assert.equal(publicApp.node('heatmapTargetSliderWrapper').style.display, 'block');
    assert.equal(publicApp.node('heatmapCompoundSliderWrapper').style.display, 'flex');
    assert.equal(publicApp.node('heatmapCompoundRangeSlider').style.width, '390px');
    assert.equal(publicApp.node('heatmapTargetRangeSlider').max, 15);
    assert.equal(publicApp.node('heatmapCompoundRangeSlider').max, 27);
    assert.equal(publicApp.node('heatmapCompoundSliderRangeText').textContent, '1–20');
    assert.equal(publicApp.node('heatmapTargetSliderRangeText').textContent, '1–40');
    assert.equal(publicApp.observers.length, 2);
    const beforeResize = publicApp.calls.filter(call => call[0] === 'resize').length;
    publicApp.observers.find(observer => observer.nodes.includes(publicApp.node('heatmapChart'))).callback();
    assert.equal(publicApp.calls.filter(call => call[0] === 'resize').length, beforeResize + 1);
});

test('a short heatmap hides unused sliders in both serving modes', async () => {
    const data = dataset(8, 12);
    for (const paged of [false, true]) {
        const app = setup(async () => assert.fail('A prefetched heatmap needs no request'), {publicService: paged});
        await app.context.loadHeatmap(paged ? tile(data) : data);
        await app.flushFrames();
        assert.equal(app.node('heatmapTargetSliderWrapper').style.display, 'none');
        assert.equal(app.node('heatmapCompoundSliderWrapper').style.display, 'none');
        const {traces: [trace], layout} = app.plots.get('heatmapChart');
        assert.deepEqual(trace.x, Array.from({length: 12}, (_, index) => index));
        assert.deepEqual(trace.y, Array.from({length: 8}, (_, index) => index));
        assert.deepEqual(layout.xaxis.range, [-0.5, 11.5]);
        assert.deepEqual(layout.yaxis.range, [7.5, -0.5]);
    }
});

test('paging uses one bounded update and retains the global color range on the last page', async () => {
    const data = dataset();
    data.compounds[46] = 'A'.repeat(34);
    let requests = 0;
    const app = setup(async url => {
        requests++;
        assert.equal(url, '/api/heatmap-data?row_offset=0&column_offset=0&row_count=100&column_count=100');
        return windowResponse(data, url);
    });
    await app.context.loadHeatmap(tile(data));
    await app.flushFrames();
    app.input('heatmapTargetRangeSlider', 15);
    app.input('heatmapCompoundRangeSlider', 27);
    await app.flushFrames();

    assert.equal(requests, 1);
    assert.equal(updates(app).length, 1);
    assert.equal(app.calls.filter(call => call[0] === 'plot').length, 1);
    const {traces: [trace], layout} = app.plots.get('heatmapChart');
    assert.deepEqual(trace.x, Array.from({length: 40}, (_, index) => index + 15));
    assert.deepEqual(trace.y, Array.from({length: 20}, (_, index) => index + 27));
    assert.deepEqual(trace.z, data.matrix.slice(27).map(row => row.slice(15)));
    assert.deepEqual([trace.zmin, trace.zmax], [-8, 9]);
    assert.equal(trace.zauto, false);
    assert.equal(Object.hasOwn(updates(app)[0][2], 'zmin'), false);
    assert.equal(Object.hasOwn(updates(app)[0][2], 'zmax'), false);
    assert.deepEqual(layout.xaxis.range, [14.5, 54.5]);
    assert.deepEqual(layout.yaxis.range, [46.5, 26.5]);
    assert.deepEqual(layout.xaxis.tickvals, trace.x);
    assert.deepEqual(layout.yaxis.tickvals, trace.y);
    assert.deepEqual(layout.xaxis.ticktext, data.targets.slice(15));
    assert.equal(layout.yaxis.ticktext[0], 'CHEMBL28');
    assert.equal(layout.yaxis.ticktext[19], 'A'.repeat(27) + '...');
    assert.deepEqual(trace.customdata[0][0], ['CHEMBL28', 'Protein target 16 (T16)']);
    assert.deepEqual(trace.customdata[19][39], ['A'.repeat(27) + '...', 'Protein target 55 (T55)']);
    assert.equal(trace.text[19][39], '9.00');
    assert.equal(app.node('heatmapTargetSliderRangeText').textContent, '16–55');
    assert.equal(app.node('heatmapCompoundSliderRangeText').textContent, '28–47');
    assert.equal(app.node('heatmapCompoundSliderStart').textContent, 'CHEMBL28');
    assert.equal(app.node('heatmapCompoundSliderEnd').textContent, 'A'.repeat(27) + '...');
    assert.equal(app.node('heatmapCompoundSliderSubtext').title, `CHEMBL28 → ${data.compounds[46]}`);

    const local = setup(async () => assert.fail('Local paging needs no request'), {publicService: false});
    await local.context.loadHeatmap(data);
    await local.flushFrames();
    local.input('heatmapTargetRangeSlider', 15);
    local.input('heatmapCompoundRangeSlider', 27);
    await local.flushFrames();
    assert.deepEqual(plain(updates(app)[0]), plain(updates(local)[0]));
});

test('a prefetched page initializes absolute axes and slider offsets', async () => {
    const data = dataset();
    const app = setup(async () => assert.fail('A prefetched page needs no request'));
    await app.context.loadHeatmap(tile(data, 27, 15));
    await app.flushFrames();
    const {traces: [trace], layout} = app.plots.get('heatmapChart');
    assert.deepEqual(layout.xaxis.range, [14.5, 54.5]);
    assert.deepEqual(layout.yaxis.range, [46.5, 26.5]);
    assert.equal(trace.x[0], 15);
    assert.equal(trace.y[0], 27);
    assert.equal(layout.xaxis.ticktext[0], 'T16');
    assert.equal(layout.yaxis.ticktext[0], 'CHEMBL28');
    assert.equal(app.node('heatmapTargetRangeSlider').value, 15);
    assert.equal(app.node('heatmapCompoundRangeSlider').value, 27);
    assert.equal(app.node('heatmapTargetSliderRangeText').textContent, '16–55');
    assert.equal(app.node('heatmapCompoundSliderRangeText').textContent, '28–47');
});

test('heatmap requests bounded windows and discards old selection responses', async () => {
    const data = dataset(200);
    const requests = [];
    const app = setup((url, options) => new Promise(resolve => requests.push({url, options, resolve})));
    await app.context.loadHeatmap(tile(data, 0, 0, {revision: 'first', total_rows: 100000}));
    await app.flushFrames();
    app.input('heatmapCompoundRangeSlider', 100);
    const pending = app.flushFrames();
    assert.equal(requests[1].url, '/api/heatmap-data?row_offset=60&column_offset=0&row_count=100&column_count=100');
    await app.context.loadHeatmap(tile(data, 0, 0, {revision: 'second', total_rows: 100000}));
    assert.equal(requests[1].options.signal.aborted, true);
    requests[1].resolve(windowResponse(data, requests[1].url, {revision: 'first', total_rows: 100000}));
    await pending;
    await app.flushFrames();
    assert.equal(updates(app).length, 0);
    assert.equal(app.calls.filter(call => call[0] === 'plot').length, 2);
    assert.equal(app.plots.get('heatmapChart').traces[0].y[0], 0);
    assert.equal(app.node('heatmapCompoundRangeSlider').max, 99980);
});

test('a tile with a different selection revision cannot update the chart', async () => {
    const data = dataset();
    const app = setup(async url => windowResponse(data, url, {revision: 'outdated'}));
    await app.context.loadHeatmap(tile(data));
    await app.flushFrames();
    app.input('heatmapCompoundRangeSlider', 10);
    await app.flushFrames();
    assert.equal(updates(app).length, 0);
    assert.equal(app.plots.get('heatmapChart').traces[0].y[0], 0);
});

test('out-of-order tile responses preserve the newest requested window', async () => {
    const data = dataset(500, 200);
    const requests = [];
    const app = setup((url, options) => new Promise(resolve => requests.push({url, options, resolve})));
    await app.context.loadHeatmap(tile(data));
    await app.flushFrames();
    app.input('heatmapCompoundRangeSlider', 140);
    const first = app.flushFrames();
    app.input('heatmapTargetRangeSlider', 125);
    const second = app.flushFrames();
    assert.deepEqual(requests.map(request => request.url), [
        '/api/heatmap-data?row_offset=0&column_offset=0&row_count=100&column_count=100',
        '/api/heatmap-data?row_offset=100&column_offset=0&row_count=100&column_count=100',
        '/api/heatmap-data?row_offset=100&column_offset=95&row_count=100&column_count=100',
    ]);
    assert.equal(requests[1].options.signal.aborted, true);
    requests[2].resolve(windowResponse(data, requests[2].url));
    await second;
    requests[1].resolve(windowResponse(data, requests[1].url));
    await first;
    assert.equal(updates(app).length, 1);
    const {traces: [trace], layout} = app.plots.get('heatmapChart');
    assert.equal(trace.x[0], 125);
    assert.equal(trace.y[0], 140);
    assert.equal(layout.xaxis.ticktext[0], 'T126');
    assert.equal(layout.yaxis.ticktext[0], 'CHEMBL141');
    assert.equal(app.node('heatmapTargetSliderRangeText').textContent, '126–165');
});

test('an older initial plot cannot replace the newest selection slider handlers', async () => {
    const data = dataset();
    let resolveFirstPlot;
    let plotCount = 0;
    const app = setup(async url => {
        assert.equal(url, '/api/heatmap-data?row_offset=0&column_offset=0&row_count=100&column_count=100');
        return windowResponse(data, url, {revision: 'new'});
    }, {newPlotReady: () => ++plotCount === 1 ? new Promise(resolve => { resolveFirstPlot = resolve; }) : undefined});
    const first = app.context.loadHeatmap(tile(data, 0, 0, {revision: 'old', total_rows: 100000}));
    await app.context.loadHeatmap(tile(data, 0, 0, {revision: 'new'}));
    resolveFirstPlot();
    await first;
    await app.flushFrames();
    app.input('heatmapCompoundRangeSlider', 10);
    await app.flushFrames();
    assert.equal(updates(app).length, 1);
    assert.equal(app.node('heatmapCompoundRangeSlider').max, 27);
    assert.equal(app.node('heatmapCompoundSliderRangeText').textContent, '11–30');
});

test('completed updates cannot overwrite labels for a newer selection or tile', async () => {
    const data = dataset(80, 100);
    for (const newer of ['selection', 'tile']) {
        let resolveFirstUpdate;
        let signalFirstUpdate;
        let updateCount = 0;
        const started = new Promise(resolve => { signalFirstUpdate = resolve; });
        const app = setup(async url => {
            return windowResponse(data, url);
        }, {updateReady: () => {
            if (++updateCount !== 1) return;
            signalFirstUpdate();
            return new Promise(resolve => { resolveFirstUpdate = resolve; });
        }});
        await app.context.loadHeatmap(tile(data));
        await app.flushFrames();
        app.input('heatmapCompoundRangeSlider', 10);
        const first = app.flushFrames();
        await started;
        if (newer === 'selection') {
            await app.context.loadHeatmap(tile(data, 0, 0, {revision: 'new'}));
        } else {
            app.input('heatmapCompoundRangeSlider', 20);
        }
        await app.flushFrames();
        resolveFirstUpdate();
        await first;
        const start = newer === 'selection' ? 0 : 20;
        assert.equal(app.plots.get('heatmapChart').traces[0].y[0], start);
        assert.equal(app.node('heatmapCompoundSliderRangeText').textContent, `${start + 1}–${start + 20}`);
        assert.equal(app.node('heatmapCompoundSliderStart').textContent, `CHEMBL${start + 1}`);
    }
});

test('a small library preloads in the background and cached dragging renders the latest view without requests',
    {timeout: 1000}, async () => {
    const data = dataset(118, 98);
    let resolvePreload;
    const app = setup(() => new Promise(() => {}), {
        preloadFetch: () => new Promise(resolve => { resolvePreload = resolve; }),
    });
    await app.context.loadHeatmap(tile(data));
    await app.flushFrames();
    assert.equal(app.requests.length, 1);
    assert.equal(app.requests[0].url, '/api/heatmap-data?preload=1');
    assert.equal(app.plots.get('heatmapChart').traces[0].y[0], 0);
    resolvePreload(Response.json(preload(data)));
    await settleBackground();

    app.input('heatmapCompoundRangeSlider', 20);
    app.input('heatmapTargetRangeSlider', 10);
    await app.flushFrames();
    assert.equal(app.plots.get('heatmapChart').traces[0].y[0], 20);
    assert.equal(app.plots.get('heatmapChart').traces[0].x[0], 10);
    const before = updates(app).length;
    for (const row of [30, 50, 70, 98]) app.input('heatmapCompoundRangeSlider', row);
    for (const column of [20, 40, 58]) app.input('heatmapTargetRangeSlider', column);
    assert.equal(app.node('heatmapCompoundSliderRangeText').textContent, '99–118');
    assert.equal(app.node('heatmapTargetSliderRangeText').textContent, '59–98');
    assert.equal(updates(app).length, before);
    await app.flushFrames();

    assert.equal(app.requests.length, 1);
    assert.equal(updates(app).length, before + 1);
    const {traces: [trace], layout} = app.plots.get('heatmapChart');
    assert.equal(trace.y[0], 98);
    assert.equal(trace.x[0], 58);
    assert.deepEqual(trace.z, data.matrix.slice(98).map(row => row.slice(58)));
    assert.equal(trace.z.length, 20);
    assert.equal(trace.z[0].length, 40);
    assert.equal(trace.customdata.length, 20);
    assert.equal(trace.customdata[0].length, 40);
    assert.deepEqual(layout.xaxis.range, [57.5, 97.5]);
    assert.deepEqual(layout.yaxis.range, [117.5, 97.5]);
    assert.deepEqual([trace.zmin, trace.zmax], [-8, 9]);
    assert.deepEqual(app.errors, []);
});

test('a completed preload renders the current drag immediately and aborts its delayed tile request',
    {timeout: 1000}, async () => {
    const data = dataset(118, 98);
    let resolvePreload;
    let resolveTile;
    const app = setup(() => new Promise(resolve => { resolveTile = resolve; }), {
        preloadFetch: () => new Promise(resolve => { resolvePreload = resolve; }),
    });
    await app.context.loadHeatmap(tile(data));
    await app.flushFrames();
    app.input('heatmapCompoundRangeSlider', 98);
    app.input('heatmapTargetRangeSlider', 58);
    assert.equal(app.node('heatmapCompoundSliderRangeText').textContent, '99–118');
    const pending = app.flushFrames();
    assert.equal(app.requests.length, 2);
    assert.equal(app.requests[1].url, '/api/heatmap-data?row_offset=18&column_offset=0&row_count=100&column_count=100');
    resolvePreload(Response.json(preload(data)));
    await settleBackground();
    await app.flushFrames();
    assert.equal(app.requests[1].options.signal.aborted, true);
    assert.equal(app.plots.get('heatmapChart').traces[0].y[0], 98);
    assert.equal(app.plots.get('heatmapChart').traces[0].x[0], 58);
    assert.equal(updates(app).length, 1);
    resolveTile(windowResponse(data, app.requests[1].url));
    await pending;
    assert.equal(updates(app).length, 1);
    assert.deepEqual(app.errors, []);
});

test('a late redundant buffer cannot evict a full preload at the 50,000-cell limit', async () => {
    const data = dataset(500, 100);
    let resolvePreload;
    let resolveTile;
    let tileRequests = 0;
    const app = setup(url => ++tileRequests === 1 ? new Promise(resolve => { resolveTile = resolve; }) :
        Promise.resolve(windowResponse(data, url)), {
        preloadFetch: () => new Promise(resolve => { resolvePreload = resolve; }),
    });
    await app.context.loadHeatmap(tile(data));
    await app.flushFrames();
    app.input('heatmapCompoundRangeSlider', 140);
    const pending = app.flushFrames();
    assert.equal(app.requests.length, 2);
    resolvePreload(Response.json(preload(data)));
    await settleBackground();
    // Let the obsolete foreground response arrive after the full matrix is
    // cached, before the next frame can abort the request and render that cache.
    assert.equal(app.requests[1].options.signal.aborted, false);
    resolveTile(windowResponse(data, app.requests[1].url));
    await pending;
    assert.equal(updates(app).length, 0);
    await app.flushFrames();
    assert.equal(app.plots.get('heatmapChart').traces[0].y[0], 140);
    app.input('heatmapCompoundRangeSlider', 400);
    app.input('heatmapTargetRangeSlider', 50);
    await app.flushFrames();
    assert.equal(app.requests.length, 2);
    assert.equal(tileRequests, 1);
    const trace = app.plots.get('heatmapChart').traces[0];
    assert.equal(trace.y[0], 400);
    assert.equal(trace.x[0], 50);
    assert.deepEqual(trace.z, data.matrix.slice(400, 420).map(row => row.slice(50, 90)));
    assert.deepEqual([trace.zmin, trace.zmax], [-8, 9]);
    assert.deepEqual(app.errors, []);
});

test('a new selection aborts its previous preload and ignores a response that still arrives', async () => {
    const oldData = dataset(118, 98);
    oldData.compounds = oldData.compounds.map(name => `OLD_${name}`);
    const data = dataset(118, 98);
    const preloads = [];
    const app = setup(async url => windowResponse(data, url, {revision: 'new'}), {
        preloadFetch: (url, options) => new Promise(resolve => preloads.push({options, resolve})),
    });
    await app.context.loadHeatmap(tile(oldData, 0, 0, {revision: 'old'}));
    await app.context.loadHeatmap(tile(data, 0, 0, {revision: 'new'}));
    await app.flushFrames();
    assert.equal(preloads.length, 2);
    assert.equal(preloads[0].options.signal.aborted, true);
    preloads[0].resolve(Response.json(preload(oldData, 'old')));
    await settleBackground();
    app.input('heatmapCompoundRangeSlider', 10);
    await app.flushFrames();
    assert.equal(app.requests.filter(request => !request.url.includes('preload=1')).length, 1);
    assert.equal(app.plots.get('heatmapChart').layout.yaxis.ticktext[0], 'CHEMBL11');
    assert.equal(app.plots.get('heatmapChart').traces[0].customdata[0][0][0], 'CHEMBL11');
    assert.deepEqual(app.errors, []);
});

test('failed or stale preloads preserve bounded slider loading and the global scale', async () => {
    const data = dataset(118, 98);
    const outcomes = [
        async () => new Response(null, {status: 503}),
        async () => { throw new Error('Network unavailable'); },
        async () => Response.json(preload(data, 'stale')),
    ];
    for (const preloadFetch of outcomes) {
        const app = setup(async url => windowResponse(data, url), {preloadFetch});
        await app.context.loadHeatmap(tile(data));
        await app.flushFrames();
        await settleBackground();
        app.input('heatmapCompoundRangeSlider', 98);
        app.input('heatmapTargetRangeSlider', 58);
        await app.flushFrames();
        assert.equal(app.requests.length, 2);
        assert.equal(app.requests[1].url, '/api/heatmap-data?row_offset=18&column_offset=0&row_count=100&column_count=100');
        assert.equal(updates(app).length, 1);
        const trace = app.plots.get('heatmapChart').traces[0];
        assert.equal(trace.y[0], 98);
        assert.equal(trace.x[0], 58);
        assert.deepEqual([trace.zmin, trace.zmax], [-8, 9]);
    }
});

test('a warmed large-library buffer makes nearby slider movement independent of delayed networking',
    {timeout: 1000}, async () => {
    const data = dataset(500, 200);
    let requests = 0;
    const app = setup(url => ++requests === 1 ? Promise.resolve(windowResponse(data, url)) : new Promise(() => {}));
    await app.context.loadHeatmap(tile(data));
    await app.flushFrames();
    await settleBackground();
    assert.equal(app.requests[0].url, '/api/heatmap-data?row_offset=0&column_offset=0&row_count=100&column_count=100');
    for (const [row, column] of [[15, 20], [45, 35], [70, 60]]) {
        app.input('heatmapCompoundRangeSlider', row);
        app.input('heatmapTargetRangeSlider', column);
        await app.flushFrames();
        const trace = app.plots.get('heatmapChart').traces[0];
        assert.equal(trace.y[0], row);
        assert.equal(trace.x[0], column);
        assert.deepEqual(trace.z, data.matrix.slice(row, row + 20).map(values => values.slice(column, column + 40)));
    }
    assert.equal(app.requests.length, 1);
    assert.equal(updates(app).length, 3);
    assert.deepEqual(app.errors, []);
});

test('several drag frames waiting for the same buffer share a request and render the latest position', async () => {
    const data = dataset();
    let resolveTile;
    const app = setup(() => new Promise(resolve => { resolveTile = resolve; }));
    await app.context.loadHeatmap(tile(data));
    await app.flushFrames();
    app.input('heatmapCompoundRangeSlider', 10);
    const first = app.flushFrames();
    app.input('heatmapCompoundRangeSlider', 20);
    const second = app.flushFrames();
    assert.equal(app.node('heatmapCompoundSliderRangeText').textContent, '21–40');
    assert.equal(app.requests.length, 2);
    assert.equal(app.requests[1].options.signal.aborted, false);
    resolveTile(windowResponse(data, app.requests[1].url));
    await Promise.all([first, second]);
    assert.equal(updates(app).length, 1);
    assert.equal(app.plots.get('heatmapChart').traces[0].y[0], 20);
    assert.equal(app.plots.get('heatmapChart').layout.yaxis.ticktext[0], 'CHEMBL21');
    assert.deepEqual(app.errors, []);
});

test('the numeric buffer cache evicts old regions before exceeding 50,000 cells', async () => {
    const data = dataset(850, 100);
    const app = setup(async url => {
        const params = new URL(url, 'http://localhost').searchParams;
        assert.equal(params.get('row_count'), '100');
        assert.equal(params.get('column_count'), '100');
        return windowResponse(data, url);
    });
    await app.context.loadHeatmap(tile(data));
    await app.flushFrames();
    await settleBackground();
    for (const row of [140, 240, 340, 440, 540]) {
        app.input('heatmapCompoundRangeSlider', row);
        await app.flushFrames();
        assert.equal(app.plots.get('heatmapChart').traces[0].y[0], row);
    }
    assert.equal(app.requests.length, 6);
    app.input('heatmapCompoundRangeSlider', 550);
    await app.flushFrames();
    assert.equal(app.requests.length, 6);
    app.input('heatmapCompoundRangeSlider', 40);
    await app.flushFrames();
    assert.equal(app.requests.length, 7);
    assert.equal(app.requests[6].url, '/api/heatmap-data?row_offset=0&column_offset=0&row_count=100&column_count=100');
    const trace = app.plots.get('heatmapChart').traces[0];
    assert.equal(trace.y[0], 40);
    assert.deepEqual([trace.zmin, trace.zmax], [-8, 9]);
    assert.deepEqual(app.errors, []);
});

test('full preloading respects both the numeric cell budget and identifier count limits', async () => {
    for (const [rows, columns, full] of [[500, 100, true], [501, 100, false], [1001, 40, false], [20, 1001, false]]) {
        const data = dataset(rows, columns);
        const app = setup(async url => windowResponse(data, url), {
            preloadFetch: async () => Response.json(preload(data)),
        });
        await app.context.loadHeatmap(tile(data));
        await app.flushFrames();
        await settleBackground();
        assert.equal(app.requests.length, 1);
        assert.equal(app.requests[0].url.includes('preload=1'), full);
        if (!full) {
            const params = new URL(app.requests[0].url, 'http://localhost').searchParams;
            assert.equal(params.get('row_count'), '100');
            assert.equal(params.get('column_count'), '100');
        }
        assert.deepEqual(app.errors, []);
    }
});

test('thin libraries keep at most five cached buffers even when their cells fit the numeric budget', async () => {
    const data = dataset(2000, 1);
    const app = setup(async url => windowResponse(data, url));
    await app.context.loadHeatmap(tile(data));
    await app.flushFrames();
    await settleBackground();
    for (const row of [140, 240, 340, 440, 540]) {
        app.input('heatmapCompoundRangeSlider', row);
        await app.flushFrames();
    }
    assert.equal(app.requests.length, 6);
    app.input('heatmapCompoundRangeSlider', 550);
    await app.flushFrames();
    assert.equal(app.requests.length, 6);
    app.input('heatmapCompoundRangeSlider', 40);
    await app.flushFrames();
    assert.equal(app.requests.length, 7);
    assert.equal(app.plots.get('heatmapChart').traces[0].y[0], 40);
    assert.deepEqual(app.errors, []);
});

test('preparing an export sends CSRF even though completed downloads use GET', async () => {
    const app = setup(async (url, options) => {
        assert.equal(options.headers.get('X-CSRFToken'), 'token');
        return new Response('workbook');
    });
    await app.context.window.fetch('/api/download/library');
});

test('cached downloads probe readiness without fetching the attachment before navigation', async () => {
    const href = '/api/download/matrix?format=csv';
    const app = setup(async (url, options) => {
        assert.equal(url, `${href}&prepare=1`);
        assert.equal(options.headers.get('X-CSRFToken'), 'token');
        return Response.json({status: 'complete', download_url: href});
    });
    const link = downloadLink(href);
    const original = [...link.childNodes];
    await app.click(link);
    assert.equal(app.requests.length, 1);
    assert.deepEqual(app.calls, [['navigate', href]]);
    assert.deepEqual(app.delays, []);
    assert.deepEqual(link.childNodes, original);
    assert.equal(link.getAttribute('aria-busy'), null);
});

test('downloads check ready jobs immediately and wait only after an incomplete status', async () => {
    for (const pending of [false, true]) {
        let polls = 0;
        const app = setup(async url => {
            if (url === '/api/download/library?prepare=1') {
                return Response.json({status_url: '/api/jobs/export'}, {status: 202});
            }
            assert.equal(url, '/api/jobs/export');
            polls++;
            assert.deepEqual(app.delays, polls === 1 ? [] : [1000]);
            return Response.json({status: pending && polls === 1 ? 'running' : 'complete'});
        });
        await app.click(downloadLink());
        assert.equal(polls, pending ? 2 : 1);
        assert.deepEqual(app.delays, pending ? [1000] : []);
        assert.deepEqual(app.calls, [['navigate', '/api/download/library']]);
    }
});

test('downloads wait for the selected solution before preparing its export', async () => {
    let selected;
    const app = setup(async url => {
        if (url === '/api/select-solution') return new Promise(resolve => { selected = resolve; });
        assert.equal(url, '/api/download/library?prepare=1');
        return Response.json({status: 'complete'});
    });
    const selection = app.context.window.fetch('/api/select-solution', {method: 'POST'});
    const link = downloadLink();
    const download = app.click(link);
    assert.equal(link.getAttribute('aria-busy'), 'true');
    assert.equal(app.requests.length, 1);
    selected(Response.json({status: 'success'}));
    await Promise.all([selection, download]);
    assert.equal(app.requests.length, 2);
    assert.deepEqual(app.calls, [['navigate', '/api/download/library']]);
    assert.equal(link.getAttribute('aria-busy'), null);
});

test('download preparation errors restore the link and do not navigate', async () => {
    for (const jobError of [false, true]) {
        const app = setup(async url => jobError && url.includes('prepare=1') ?
            Response.json({status_url: '/api/jobs/export'}, {status: 202}) :
            Response.json({status: 'error', error: 'Export unavailable'}, {status: jobError ? 200 : 409}));
        const link = downloadLink();
        const original = [...link.childNodes];
        await app.click(link);
        assert.equal(app.node('publicJobNotice').textContent, 'Export unavailable');
        assert.deepEqual(app.calls, []);
        assert.deepEqual(link.childNodes, original);
        assert.equal(link.getAttribute('aria-busy'), null);
    }
});
