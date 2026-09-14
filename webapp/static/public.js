/* Public-service adapters: asynchronous jobs, bounded pages and heatmap tiles. */
(() => {
    const apiFetch = window.fetch;
    let pendingSelections = 0;
    let sessionId = document.querySelector('meta[name="optilib-session"]')?.content;
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const shouldWait = url => /\/api\/(upload-|remove-|clear-|select-solution)/.test(String(url));
    async function waitForJob(data, signal) {
        while (true) {
            if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
            await sleep(1000);
            const response = await apiFetch(data.status_url, {signal});
            const job = await response.json();
            if (!response.ok) throw new Error(job.error || 'Could not check job status.');
            if (job.status === 'complete') return job.result;
            if (['error', 'cancelled'].includes(job.status)) throw new Error(job.error || 'The job was cancelled.');
        }
    }
    window.fetch = async (url, options = {}) => {
        const isSelection = String(url) === '/api/select-solution';
        if (isSelection) pendingSelections++;
        try {
        if (String(url).startsWith('/api/download/')) {
            options = {...options, headers: new Headers(options.headers || {})};
            options.headers.set('X-CSRFToken', document.querySelector('meta[name="csrf-token"]')?.content || '');
        }
        const response = await apiFetch(url, options);
        const serverSession = response.headers.get('X-Optilib-Session');
        if (serverSession && sessionId && serverSession !== sessionId && String(url) !== '/api/reset') {
            sessionStorage.clear();
            window.location.reload();
            throw new Error('Your temporary session ended.');
        }
        if (serverSession) {
            sessionId = serverSession;
            const previous = sessionStorage.getItem('optilibSession');
            if (previous && previous !== sessionId && String(url) !== '/api/reset') {
                sessionStorage.clear();
                sessionStorage.setItem('optilibSession', sessionId);
                window.location.reload();
                throw new Error('Your temporary session ended.');
            }
            sessionStorage.setItem('optilibSession', sessionId);
        }
        if (response.status === 202 && shouldWait(url)) {
            try {
                const result = await waitForJob(await response.json(), options.signal);
                return new Response(JSON.stringify(result), {status: 200, headers: {'Content-Type': 'application/json'}});
            } catch (error) {
                if (error.name === 'AbortError') throw error;
                return new Response(JSON.stringify({error: error.message}), {status: 400, headers: {'Content-Type': 'application/json'}});
            }
        }
        return response;
        } finally {
            if (isSelection) pendingSelections--;
        }
    };

    // Async downloads remain links for accessibility, with work shown on click.
    document.querySelectorAll('a[href^="/api/download/"]').forEach(link => {
        const csv = link.cloneNode(true);
        csv.removeAttribute('id');
        csv.href = `${link.getAttribute('href')}?format=csv`;
        csv.textContent = 'Download CSV';
        link.after(csv);
    });
    document.addEventListener('click', async event => {
        const link = event.target.closest('a[href^="/api/download/"]');
        if (!link) return;
        event.preventDefault();
        if (link.getAttribute('aria-busy') === 'true') return;
        const original = link.textContent;
        link.setAttribute('aria-busy', 'true'); link.textContent = 'Preparing download…';
        try {
            while (pendingSelections) await sleep(100);
            const response = await window.fetch(link.getAttribute('href'));
            if (response.status === 202) await waitForJob(await response.json());
            else if (!response.ok) throw new Error((await response.json()).error || 'Download failed.');
            // Navigate to the completed artifact; never buffer a large blob in JS.
            window.location.assign(link.getAttribute('href'));
        } catch (error) {
            let notice = document.getElementById('publicJobNotice');
            if (!notice) {
                notice = document.createElement('p'); notice.id = 'publicJobNotice';
                notice.className = 'session-notice'; notice.setAttribute('role', 'alert');
                document.querySelector('.workspace-header').append(notice);
            }
            notice.textContent = error.message;
        } finally {
            link.textContent = original; link.removeAttribute('aria-busy');
        }
    });

    let heatmapRequest = 0;
    loadHeatmap = async function(prefetched) {
        const requestId = ++heatmapRequest;
        const initial = prefetched || await (await window.fetch('/api/heatmap-data')).json();
        if (!initial.paged || requestId !== heatmapRequest) return;
        const revision = initial.revision;
        let tileRequest = 0;
        const targetSlider = document.getElementById('heatmapTargetRangeSlider');
        const compoundSlider = document.getElementById('heatmapCompoundRangeSlider');
        const shortLabel = value => value.length > 30 ? value.slice(0, 27) + '…' : value;
        const labels = tile => ({
            x: tile.targets.map((_, i) => i + tile.column_offset),
            y: tile.compounds.map((_, i) => i + tile.row_offset), z: tile.matrix,
            customdata: tile.compounds.map(c => tile.targets.map(t => [c, t])),
        });
        const tile = labels(initial);
        let low = initial.zmin, high = initial.zmax;
        if (low === high) { low -= .5; high += .5; }
        await Plotly.newPlot('heatmapChart', [{...tile, type: 'heatmap', zmin: low, zmax: high,
            colorscale: 'Viridis', hovertemplate: 'Target: %{customdata[1]}<br>Compound: %{customdata[0]}<br>Selectivity: %{z:.2f}<extra></extra>'}], {
            paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font: {color: '#9898b8'},
            xaxis: {tickmode: 'array', tickvals: tile.x, ticktext: initial.targets.map(shortLabel), side: 'top', automargin: true},
            yaxis: {tickmode: 'array', tickvals: tile.y, ticktext: initial.compounds.map(shortLabel), autorange: 'reversed', automargin: true},
            margin: {l: 150, r: 50, t: 120, b: 30},
        }, {responsive: true, displayModeBar: false});
        function ranges(data) {
            for (const [prefix, offset, length, total] of [
                ['heatmapTargetSlider', data.column_offset, data.targets.length, data.total_columns],
                ['heatmapCompoundSlider', data.row_offset, data.compounds.length, data.total_rows],
            ]) {
                for (const [suffix, value] of [['RangeText', `${offset + 1}–${offset + length}`],
                    ['TotalText', `of ${total}`], ['MinLabel', '1'], ['MaxLabel', total]]) {
                    const node = document.getElementById(prefix + suffix);
                    if (node) node.textContent = value;
                }
            }
        }
        ranges(initial);
        async function update() {
            const sequence = ++tileRequest;
            const ro = Number(compoundSlider?.value || 0), co = Number(targetSlider?.value || 0);
            const response = await window.fetch(`/api/heatmap-data?row_offset=${ro}&column_offset=${co}`);
            if (!response.ok) return;
            const data = await response.json();
            if (requestId !== heatmapRequest || sequence !== tileRequest || data.revision !== revision) return;
            ranges(data);
            const next = labels(data);
            await Plotly.restyle('heatmapChart', {x: [next.x], y: [next.y], z: [next.z], customdata: [next.customdata]});
            await Plotly.relayout('heatmapChart', {'xaxis.tickvals': next.x, 'xaxis.ticktext': data.targets.map(shortLabel),
                'yaxis.tickvals': next.y, 'yaxis.ticktext': data.compounds.map(shortLabel)});
            for (const [id, value] of [['targetStartText', co + 1], ['targetEndText', co + data.targets.length],
                ['compoundStartText', ro + 1], ['compoundEndText', ro + data.compounds.length]]) {
                const node = document.getElementById(id); if (node) node.textContent = value;
            }
        }
        for (const [slider, total, count, wrapper] of [
            [targetSlider, initial.total_columns, 40, 'heatmapTargetSliderWrapper'],
            [compoundSlider, initial.total_rows, 20, 'heatmapCompoundSliderWrapper'],
        ]) {
            if (!slider) continue;
            slider.min = 0; slider.max = Math.max(0, total - count); slider.value = 0;
            slider.oninput = () => update().catch(console.error);
            const node = document.getElementById(wrapper); if (node) node.style.display = total > count ? '' : 'none';
        }
    };

    const originalComparison = loadComparison;
    let compoundOffset = 0;
    loadComparison = async function() {
        compoundOffset = 0;
        await originalComparison();
        const table = document.getElementById('compoundsListBody')?.closest('table');
        if (!table) return;
        document.getElementById('compoundPaging')?.remove();
        const controls = document.createElement('div'); controls.id = 'compoundPaging'; controls.className = 'public-pagination';
        const previous = document.createElement('button'), next = document.createElement('button'), label = document.createElement('span');
        previous.className = next.className = 'btn btn-secondary';
        previous.textContent = 'Previous compounds'; next.textContent = 'Next compounds';
        controls.append(previous, label, next); table.after(controls);
        async function page(offset) {
            const response = await window.fetch(`/api/results?offset=${offset}&limit=100`);
            if (!response.ok) return;
            const data = await response.json();
            compoundOffset = offset;
            const body = document.getElementById('compoundsListBody'); body.replaceChildren();
            const custom = data.comparison.has_custom_affinity;
            for (const item of data.comparison.library.compounds) {
                const row = document.createElement('tr');
                const values = custom ? [item.name,item.chembl_id,item.inchikey,`$${item.price.toFixed(2)}`] : [item.chembl_id,item.inchikey,`$${item.price.toFixed(2)}`];
                values.forEach(value => { const cell = document.createElement('td'); cell.textContent = value || '—'; row.append(cell); });
                body.append(row);
            }
            label.textContent = `${offset + 1}–${Math.min(offset + 100, data.total)} of ${data.total}`;
            previous.disabled = offset === 0; next.disabled = offset + 100 >= data.total;
        }
        previous.onclick = () => page(Math.max(0, compoundOffset - 100)).catch(console.error);
        next.onclick = () => page(compoundOffset + 100).catch(console.error);
        await page(0);
    };

    for (const [kind, listId, render] of [
        ['affinity', 'affinityCompoundList', renderAffinityFiles],
        ['prices', 'priceCompoundList', renderPriceFiles],
    ]) {
        const list = document.getElementById(listId);
        if (!list) continue;
        const controls = document.createElement('div'); controls.className = 'public-pagination';
        const previous = document.createElement('button'), next = document.createElement('button'), label = document.createElement('span');
        previous.className = next.className = 'btn btn-secondary';
        previous.textContent = 'Previous compounds'; next.textContent = 'Next compounds';
        controls.append(previous, label, next); list.after(controls);
        let offset = 0;
        async function page(nextOffset) {
            const response = await window.fetch(`/api/uploads/${kind}?offset=${nextOffset}&limit=100`);
            if (!response.ok) return;
            const data = await response.json();
            offset = nextOffset;
            applyUploadResponse(kind === 'prices' ? 'price' : kind, data);
            render();
            label.textContent = `${offset + 1}–${Math.min(offset + 100, data.total)} of ${data.total}`;
            previous.disabled = offset === 0; next.disabled = offset + 100 >= data.total;
        }
        previous.addEventListener('click', () => page(Math.max(0, offset - 100)).catch(console.error));
        next.addEventListener('click', () => page(offset + 100).catch(console.error));
    }
})();
