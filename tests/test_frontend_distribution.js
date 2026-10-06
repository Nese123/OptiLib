// Run with: node --test tests/test_frontend_distribution.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../webapp/static/app.js'), 'utf8');
const distributionSource = source.slice(source.indexOf('async function loadDistributionChart('),
    source.indexOf('// Navigation buttons on step 4'));
const plain = value => JSON.parse(JSON.stringify(value));

function setup(fetch = async url => assert.fail(`Unexpected request: ${url}`)) {
    const nodes = new Map();
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, {value: 0, style: {}, textContent: ''});
        return nodes.get(id);
    };
    const calls = [];
    const requests = [];
    const errors = [];
    let plot;
    const context = vm.createContext({
        $: selector => node(selector.slice(1)),
        fetch: (...args) => { requests.push(args); return fetch(...args); },
        console: {...console, error: (...args) => errors.push(args)},
        Plotly: {
            newPlot: async (id, traces, layout, config) => {
                calls.push(['plot', id, traces, layout, config]);
                plot = plain({traces, layout, config});
            },
            relayout: async (id, updates) => {
                calls.push(['relayout', id, updates]);
                for (const [key, value] of Object.entries(updates)) {
                    const [axis, property] = key.split('.');
                    plot.layout[axis][property] = plain(value);
                }
            },
        },
    });
    vm.runInContext(distributionSource, context);
    const input = value => {
        const slider = node('distributionRangeSlider');
        slider.value = value;
        slider.oninput({target: slider});
    };
    return {context, calls, requests, errors, node, input, get plot() { return plot; }};
}

function smallLibrary() {
    const columns = [
        {target: 'POS', target_name: 'Positive enzyme', values: [1, 3, 5, 7], min: 1, median: 4, max: 7},
        {target: 'MIX', target_name: 'Mixed enzyme', values: [-4, -1, 3, 6], min: -4, median: 1, max: 6},
        {target: 'NEG', target_name: 'Negative enzyme', values: [-9, -7, -5, -3], min: -9, median: -6, max: -3},
        {target: 'EVEN', target_name: 'Even negative enzyme', values: [-6, -2, null, null], min: -6, median: -4, max: -2},
        {target: 'NONE', target_name: 'No data', values: [null, null, null, null]},
        {target: 'SAME', target_name: 'Zero enzyme', values: [0, 0, 0, 0], min: 0, median: 0, max: 0},
        {target: 'SYMBOL', target_name: 'SYMBOL', values: [null, 2, 4, null], min: 2, median: 3, max: 4},
        {target: 'INNER', target_name: 'Kinase (stress activated)', values: [2, 4, 6, 8], min: 2, median: 5, max: 8},
        {target: 'Unmapped custom target', target_name: 'Unmapped custom target', values: [-2, 0, 2, 4], min: -2, median: 1, max: 4},
    ];
    return {
        data: {
            matrix: Array.from({length: 4}, (_, row) => columns.map(column => column.values[row])),
            compounds: ['A', 'B', 'C', 'D'],
            targets: columns.map(column => column.target),
            target_names: columns.map(column => column.target_name),
        },
        distribution: columns.filter(column => column.max !== undefined)
            .map(({target, target_name, min, median, max}) => ({target, target_name, min, median, max})),
    };
}

function largeLibrary() {
    const matrix = Array.from({length: 25}, (_, row) => Array.from({length: 45}, (_, column) =>
        column === 44 ? null : row + column));
    matrix[0][0] = -1000;
    matrix[24][43] = 100;
    return {
        data: {
            matrix,
            compounds: Array.from({length: 25}, (_, row) => `C${row + 1}`),
            targets: Array.from({length: 45}, (_, column) => `T${column + 1}`),
            target_names: Array.from({length: 45}, (_, column) => `Target protein ${column + 1}`),
        },
        distribution: Array.from({length: 44}, (_, column) => ({
            target: `T${column + 1}`, target_name: `Target protein ${column + 1}`,
            min: column === 0 ? -1000 : column, median: column + 12, max: column === 43 ? 100 : column + 24,
        })),
    };
}

function paged(data, distribution) {
    return {
        paged: true, revision: 'current', row_offset: 0, column_offset: 0,
        total_rows: data.compounds.length, total_columns: data.targets.length,
        matrix: data.matrix.slice(0, 20).map(row => row.slice(0, 40)),
        compounds: data.compounds.slice(0, 20), targets: data.targets.slice(0, 40),
        target_names: data.target_names.slice(0, 40), distribution,
    };
}

test('local and precomputed distributions render identical signed segments, gene labels, hover names and styles', async () => {
    const {data, distribution} = smallLibrary();
    const local = setup();
    const docker = setup();
    await local.context.loadDistributionChart(data);
    await docker.context.loadDistributionChart(paged(data, distribution));
    assert.deepEqual(docker.plot, local.plot);
    const {traces: [maximum, median, minimum], layout, config} = docker.plot;
    assert.deepEqual(layout.xaxis.ticktext,
        ['INNER', 'POS', 'MIX', 'SYMBOL', 'Unmapped custom target', 'SAME', 'EVEN', 'NEG']);
    assert.deepEqual(maximum.base, [5, 4, 1, 3, 1, 0, -2, -3]);
    assert.deepEqual(maximum.y, [3, 3, 5, 1, 3, 0, 2, 3]);
    assert.deepEqual(median.base, [2, 1, 0, 2, 0, 0, -4, -6]);
    assert.deepEqual(median.y, [3, 3, 1, 1, 1, 0, 2, 3]);
    assert.deepEqual(minimum.base, [0, 0, -4, 0, -2, 0, -6, -9]);
    assert.deepEqual(minimum.y, [2, 1, 4, 2, 2, 0, 2, 3]);
    assert.deepEqual(maximum.customdata[0], [8, 5, 2, 'INNER', 'Kinase (stress activated) (INNER)']);
    assert.deepEqual(maximum.customdata[2], [6, 1, -4, 'MIX', 'Mixed enzyme (MIX)']);
    assert.equal(maximum.customdata[3][4], 'SYMBOL');
    assert.equal(maximum.customdata[4][4], 'Unmapped custom target');
    assert.deepEqual(maximum.customdata, median.customdata);
    assert.deepEqual(maximum.customdata, minimum.customdata);
    assert.equal(layout.xaxis.ticktext.includes('NONE'), false);
    assert.deepEqual([maximum, median, minimum].map(trace => trace.marker.color),
        ['rgba(132, 94, 247, 0.8)', 'rgba(77, 171, 247, 0.85)', 'rgba(56, 217, 169, 0.95)']);
    for (const trace of docker.plot.traces) {
        assert.equal(trace.type, 'bar');
        assert.equal(trace.width, 0.85);
        assert.match(trace.hovertemplate, /customdata\[4\]/);
        assert.match(trace.hovertemplate, /Max:.*Median:.*Min:/);
    }
    assert.equal(layout.barmode, 'overlay');
    assert.equal(layout.xaxis.tickangle, -45);
    assert.equal(layout.yaxis.title.text, 'Selectivity Score');
    assert.deepEqual(layout.margin, {l: 55, r: 120, t: 20, b: 90});
    assert.equal(config.responsive, true);
    assert.equal(config.displayModeBar, false);
    assert.equal(docker.node('distributionSliderWrapper').style.display, 'none');
    assert.equal(local.node('distributionSliderWrapper').style.display, 'none');
    assert.deepEqual(docker.requests, []);
    assert.deepEqual(local.requests, []);
    assert.deepEqual(docker.errors, []);
    assert.deepEqual(local.errors, []);
});

test('precomputed stats cover every target and compound beyond the heatmap tile and sort by full-library maxima', async () => {
    const {data, distribution} = largeLibrary();
    const payload = paged(data, [...distribution].reverse());
    assert.equal(payload.matrix.length, 20);
    assert.equal(payload.targets.length, 40);
    const local = setup();
    const docker = setup();
    await local.context.loadDistributionChart(data);
    await docker.context.loadDistributionChart(payload);
    assert.deepEqual(docker.plot, local.plot);
    const {traces: [maximum], layout} = docker.plot;
    assert.equal(maximum.x.length, 44);
    assert.deepEqual(layout.xaxis.ticktext, Array.from({length: 44}, (_, index) => `T${44 - index}`));
    assert.deepEqual(maximum.customdata[0], [100, 55, 43, 'T44', 'Target protein 44 (T44)']);
    assert.deepEqual(maximum.customdata[43], [24, 12, -1000, 'T1', 'Target protein 1 (T1)']);
    assert.equal(layout.xaxis.ticktext.includes('T45'), false);
    assert.deepEqual(layout.xaxis.range, [-0.5, 39.5]);
    assert.equal(docker.node('distributionSliderWrapper').style.display, 'block');
    assert.equal(docker.node('distributionRangeSlider').max, 4);
    assert.equal(docker.node('distributionSliderTotalText').textContent, 'of 44');
    assert.equal(docker.node('distributionSliderRangeText').textContent, '1–40');
    assert.deepEqual(docker.errors, []);
});

test('legacy canonical names preserve compact symbols and complete hover labels while unknown targets stay unchanged', async () => {
    const app = setup();
    await app.context.loadDistributionChart({matrix: [[null]], targets: ['tile'], distribution: [
        {target: 'Kinase (stress activated) (MAPK14)', min: -2, median: 1, max: 7},
        {target: 'Old preferred name (LEGACY-1)', target_name: 'Current preferred name', min: 2, median: 4, max: 5},
        {target: 'Unmapped custom target', min: 0, median: 0, max: 0},
    ]});
    assert.deepEqual(app.plot.layout.xaxis.ticktext, ['MAPK14', 'LEGACY-1', 'Unmapped custom target']);
    assert.deepEqual(app.plot.traces[0].customdata.map(values => values[4]), [
        'Kinase (stress activated) (MAPK14)', 'Current preferred name (LEGACY-1)', 'Unmapped custom target',
    ]);
    assert.deepEqual(app.errors, []);
});

test('distribution sliders reuse cached traces and preserve the global score axis without HTTP', async () => {
    const {data, distribution} = largeLibrary();
    for (const payload of [data, paged(data, distribution)]) {
        const app = setup();
        await app.context.loadDistributionChart(payload);
        const traces = plain(app.plot.traces);
        const scoreAxis = plain(app.plot.layout.yaxis);
        // Plotly computes this score range from every cached bar, including the
        // -1000 minimum outside the first 40-target viewport.
        const scoreExtent = chart => {
            const bounds = chart.traces.flatMap(trace => trace.base.flatMap((base, index) => [base, base + trace.y[index]]));
            return [Math.min(...bounds), Math.max(...bounds)];
        };
        assert.deepEqual(scoreExtent(app.plot), [-1000, 100]);
        for (const offset of [1, 3, 4, 0]) {
            app.input(offset);
            assert.equal(app.node('distributionSliderRangeText').textContent, `${offset + 1}–${offset + 40}`);
            assert.deepEqual(app.plot.layout.xaxis.range, [offset - 0.5, offset + 39.5]);
            assert.deepEqual(app.plot.traces, traces);
            assert.deepEqual(app.plot.layout.yaxis, scoreAxis);
            assert.deepEqual(scoreExtent(app.plot), [-1000, 100]);
        }
        assert.equal(app.calls.filter(call => call[0] === 'plot').length, 1);
        const changes = app.calls.filter(call => call[0] === 'relayout');
        assert.equal(changes.length, 4);
        for (const change of changes) assert.deepEqual(Object.keys(change[2]), ['xaxis.range']);
        assert.deepEqual(app.requests, []);
        assert.deepEqual(app.errors, []);
    }
});

test('loading distribution data fetches once and later slider movement stays local', async () => {
    const {data, distribution} = largeLibrary();
    const app = setup(async url => {
        assert.equal(url, '/api/heatmap-data');
        return Response.json(paged(data, distribution));
    });
    await app.context.loadDistributionChart();
    app.input(4);
    app.input(0);
    assert.equal(app.requests.length, 1);
    assert.equal(app.plot.traces[0].x.length, 44);
    assert.deepEqual(app.errors, []);
});
