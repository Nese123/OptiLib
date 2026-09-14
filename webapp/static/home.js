const navbar = document.getElementById('navbar');

        // ─── Scroll-triggered animations ───
        const observerOptions = {
            threshold: 0.15,
            rootMargin: '0px 0px -50px 0px'
        };

        const observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('visible');
                    observer.unobserve(entry.target);
                }
            });
        }, observerOptions);

        document.querySelectorAll('.viz-item, .setup-step-card, .opt-cockpit-panel, .opt-cockpit-chart-card, .opt-budget-card, .opt-advanced-card').forEach(el => {
            el.classList.add('animate-on-scroll');
            observer.observe(el);
        });

        // ─── Optimization Parameter Simulator ───
        const meanSlider = document.getElementById('optWeightMean');
        const minSlider = document.getElementById('optWeightMin');
        const meanVal = document.getElementById('optWeightMeanVal');
        const minVal = document.getElementById('optWeightMinVal');
        const formulaW1 = document.getElementById('optFormulaW1');
        const formulaW2 = document.getElementById('optFormulaW2');
        const missingSlider = document.getElementById('optMissing');
        const missingVal = document.getElementById('optMissingVal');

        if (meanSlider && minSlider) {
            meanSlider.addEventListener('input', () => {
                const val = parseFloat(meanSlider.value);
                meanVal.textContent = val.toFixed(2);
                const compVal = Math.max(0, 1.0 - val);
                minSlider.value = compVal.toFixed(2);
                minVal.textContent = compVal.toFixed(2);
                if (formulaW1) formulaW1.textContent = val.toFixed(2);
                if (formulaW2) formulaW2.textContent = compVal.toFixed(2);
            });

            minSlider.addEventListener('input', () => {
                const val = parseFloat(minSlider.value);
                minVal.textContent = val.toFixed(2);
                const compVal = Math.max(0, 1.0 - val);
                meanSlider.value = compVal.toFixed(2);
                meanVal.textContent = compVal.toFixed(2);
                if (formulaW1) formulaW1.textContent = compVal.toFixed(2);
                if (formulaW2) formulaW2.textContent = val.toFixed(2);
            });
        }

        if (missingSlider && missingVal) {
            missingSlider.addEventListener('input', () => {
                missingVal.textContent = missingSlider.value + '%';
            });
        }

        const ftolSlider = document.getElementById('optFtol');
        const ftolVal = document.getElementById('optFtolVal');

        if (ftolSlider && ftolVal) {
            ftolSlider.addEventListener('input', () => {
                const val = parseFloat(ftolSlider.value);
                ftolVal.textContent = val.toFixed(4);
                updateDemoHistoryChart(val);
            });
        }

        // ─── Navbar appearance and active section ───
        const spySections = [
            { id: 'set-up', href: '#set-up' },
            { id: 'optimization', href: '#optimization' },
            { id: 'visualizations', href: '#visualizations' },
            { id: 'references', href: '#references' },
        ].map(section => ({ ...section, element: document.getElementById(section.id) }));

        const allNavLinks = Array.from(document.querySelectorAll('.navbar-links .nav-link:not(.nav-cta), .navbar-mobile-menu .nav-link:not(.nav-cta)'), element => ({
            element,
            href: element.getAttribute('href'),
        }));
        let navUpdatePending = false;
        let navbarScrolled = navbar.classList.contains('scrolled');
        let lastActiveHref = null;

        function updateNavigation() {
            navUpdatePending = false;
            // Finish geometry reads before updating either navigation state.
            const scrollY = window.scrollY;
            const navHeight = navbar.offsetHeight;
            const scrollOffset = navHeight + 80;
            const isBottom = (window.innerHeight + scrollY) >= (document.documentElement.scrollHeight - 50);
            const scrolled = scrollY > 40;

            let activeHref = '#'; // default to Home

            if (isBottom) {
                activeHref = '#references';
            } else {
                for (const sec of spySections) {
                    if (sec.element) {
                        const top = sec.element.offsetTop - scrollOffset;
                        if (scrollY >= top) {
                            activeHref = sec.href;
                        }
                    }
                }
            }

            if (scrolled !== navbarScrolled) {
                navbar.classList.toggle('scrolled', scrolled);
                navbarScrolled = scrolled;
            }
            if (activeHref !== lastActiveHref) {
                allNavLinks.forEach(({ element, href }) => {
                    const active = href === activeHref;
                    if (element.classList.contains('active') !== active) {
                        element.classList.toggle('active', active);
                    }
                });
                lastActiveHref = activeHref;
            }
        }

        function scheduleNavigationUpdate() {
            if (navUpdatePending) return;
            navUpdatePending = true;
            requestAnimationFrame(updateNavigation);
        }

        window.addEventListener('scroll', scheduleNavigationUpdate, { passive: true });
        window.addEventListener('resize', scheduleNavigationUpdate);
        window.addEventListener('pageshow', scheduleNavigationUpdate);
        updateNavigation();

        // ─── Floating particles background ───
        function createParticles() {
            const container = document.getElementById('heroParticles');
            if (!container) return;
            const particleCount = 15;
            for (let i = 0; i < particleCount; i++) {
                const particle = document.createElement('div');
                particle.className = 'hero-particle';
                particle.style.left = Math.random() * 100 + '%';
                particle.style.top = Math.random() * 100 + '%';
                particle.style.width = (Math.random() * 4 + 2) + 'px';
                particle.style.height = particle.style.width;
                particle.style.animationDelay = (Math.random() * 6) + 's';
                particle.style.animationDuration = (Math.random() * 8 + 6) + 's';
                container.appendChild(particle);
            }
        }
        createParticles();

        const heroSection = document.querySelector('.hero-section');
        const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
        let heroVisible = false;

        function updateHeroMotion() {
            heroSection.classList.toggle('hero-motion-active', heroVisible && !document.hidden && !reducedMotion.matches);
        }

        const heroObserver = new IntersectionObserver(entries => {
            heroVisible = entries[entries.length - 1].isIntersecting;
            updateHeroMotion();
        });
        heroObserver.observe(heroSection);
        document.addEventListener('visibilitychange', updateHeroMotion);
        reducedMotion.addEventListener('change', updateHeroMotion);

        // ─── Demo Visualization Charts ───
        function initDemoParetoChart() {
            const chartEl = document.getElementById('demoParetoChart');
            if (!chartEl) return;

            const rawPoints = [
                { x: 0.68, y: 310 },
                { x: 0.74, y: 380 },
                { x: 0.82, y: 460 },
                { x: 0.91, y: 550 },
                { x: 1.02, y: 680 },
                { x: 1.12, y: 840 },
                { x: 1.22, y: 1050 },
                { x: 1.31, y: 1300 }, // Best compromise index 7
                { x: 1.40, y: 1620 },
                { x: 1.48, y: 1980 },
                { x: 1.55, y: 2400 },
                { x: 1.62, y: 2900 },
                { x: 1.67, y: 3450 },
                { x: 1.71, y: 4100 },
                { x: 1.74, y: 4800 },
            ];

            const bestIdx = 7;
            const selectedIdx = 7;
            const x = rawPoints.map(p => p.x);
            const y = rawPoints.map(p => p.y);

            const hoverTemplates = rawPoints.map((_, i) => {
                if (i === bestIdx) return 'Selectivity: %{x:.2f}<br>Cost: $%{y:,.0f}<br><i>Best compromise (click to select)</i><extra></extra>';
                return 'Selectivity: %{x:.2f}<br>Cost: $%{y:,.0f}<br><i>Click to select</i><extra></extra>';
            });

            const hoverBgColors = rawPoints.map((_, i) => {
                if (i === bestIdx) return '#845ef7';
                return 'rgba(56, 217, 169, 0.95)';
            });

            const traceAll = {
                x: x,
                y: y,
                mode: 'markers',
                type: 'scatter',
                name: 'Pareto Solutions',
                customdata: rawPoints.map((_, i) => i),
                marker: {
                    size: 10,
                    color: 'rgba(56, 217, 169, 0.85)',
                    symbol: 'circle',
                    line: { width: 1.5, color: '#12f3b9' },
                },
                hovertemplate: hoverTemplates,
                hoverlabel: {
                    bgcolor: hoverBgColors,
                    font: { color: '#070712', size: 12, family: 'Inter, sans-serif' },
                    bordercolor: 'transparent',
                },
            };

            const traceBest = {
                x: [x[bestIdx]],
                y: [y[bestIdx]],
                mode: 'markers',
                type: 'scatter',
                name: 'Best Compromise',
                marker: {
                    size: 14,
                    color: '#845ef7',
                    symbol: 'star',
                    line: { width: 1.5, color: '#fff' },
                },
                hoverinfo: 'skip',
            };

            const traceSelected = {
                x: [x[selectedIdx]],
                y: [y[selectedIdx]],
                mode: 'markers',
                type: 'scatter',
                name: 'Selected',
                marker: {
                    size: 18,
                    color: 'rgba(0,0,0,0)',
                    symbol: 'circle',
                    line: { width: 3, color: '#ffd43b' },
                },
                hoverinfo: 'skip',
            };

            const layout = {
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0.15)',
                font: { family: 'Inter, sans-serif', color: '#9898b8' },
                xaxis: {
                    title: { text: 'Selectivity Score', font: { size: 12, color: '#9898b8' }, standoff: 15 },
                    gridcolor: 'rgba(120, 120, 255, 0.08)',
                    zerolinecolor: 'rgba(120, 120, 255, 0.12)',
                    automargin: true,
                    fixedrange: true,
                },
                yaxis: {
                    title: { text: 'Total Library Cost (USD)', font: { size: 12, color: '#9898b8' }, standoff: 15 },
                    gridcolor: 'rgba(120, 120, 255, 0.08)',
                    zerolinecolor: 'rgba(120, 120, 255, 0.12)',
                    tickformat: '$,.0f',
                    fixedrange: true,
                },
                legend: {
                    font: { size: 10 },
                    bgcolor: 'rgba(0,0,0,0.4)',
                    bordercolor: 'rgba(120,120,255,0.15)',
                    borderwidth: 1,
                    x: 0.02,
                    y: 0.98,
                    xanchor: 'left',
                    yanchor: 'top',
                },
                margin: { l: 75, r: 25, t: 20, b: 65 },
            };

            Plotly.newPlot('demoParetoChart', [traceAll, traceBest, traceSelected], layout, {
                responsive: true,
                displayModeBar: false,
                scrollZoom: false,
                doubleClick: false,
            });

            chartEl.on('plotly_click', (eventData) => {
                const point = eventData.points[0];
                if (point.curveNumber !== 0) return;
                const clickedIdx = point.customdata;
                Plotly.restyle('demoParetoChart', {
                    x: [[x[clickedIdx]]],
                    y: [[y[clickedIdx]]],
                }, [2]);
            });
        }

        function initDemoHeatmapChart() {
            const chartEl = document.getElementById('demoHeatmapChart');
            if (!chartEl) return;

            const targets = ['EGFR', 'BRAF', 'ERBB2', 'PIK3CA', 'CDK4', 'MTOR', 'JAK2', 'ALK', 'MET', 'KDR'];
            const compounds = ['CHEMBL25', 'CHEMBL1800', 'CHEMBL278', 'CHEMBL428', 'CHEMBL192', 'CHEMBL554', 'CHEMBL392', 'CHEMBL762', 'CHEMBL901', 'CHEMBL1122'];
            const matrix = [
                [1.85, 0.42, -0.30, null, 0.30, 0.55, 0.40, 1.45, -0.20, 0.88],
                [-0.35, 1.92, -0.55, 0.85, null, 0.40, 1.30, null, -0.50, -0.70],
                [1.70, null, 1.88, -0.45, 0.60, 0.70, null, 1.35, -0.30, 0.95],
                [0.40, 0.55, -0.60, 1.75, 0.50, 1.40, 0.45, null, 0.90, -0.35],
                [-0.50, null, -0.45, 0.35, 1.82, 0.60, 0.50, 0.45, null, -0.40],
                [0.60, 0.45, -0.35, 1.35, 0.40, 1.95, 0.35, 0.50, 0.85, -0.60],
                [null, 1.25, -0.40, -0.30, 0.65, 0.60, 1.80, 0.60, -0.65, -0.45],
                [1.40, null, 1.30, 0.45, 0.35, 0.50, 0.40, 1.90, -0.60, 0.80],
                [-0.20, 0.80, 0.90, -0.45, 0.45, 0.90, 0.65, 1.15, 1.75, -0.50],
                [0.90, 0.35, 0.85, 0.40, 0.50, 0.70, 0.25, 0.75, -0.40, 1.85]
            ];

            const hoverText = matrix.map(row =>
                row.map(val => val === null ? "No Data" : val.toFixed(2))
            );

            const customData = matrix.map((row, i) =>
                row.map((_, j) => [compounds[i], targets[j]])
            );

            const trace = {
                z: matrix,
                x: targets,
                y: compounds,
                text: hoverText,
                customdata: customData,
                type: 'heatmap',
                colorscale: 'Viridis',
                hovertemplate: 'Target: %{customdata[1]}<br>Compound: %{customdata[0]}<br>Selectivity: %{text}<extra></extra>',
                colorbar: {
                    tickfont: { color: '#9898b8', size: 10 },
                    len: 0.9,
                    thickness: 18,
                    x: 1.08,
                    xanchor: 'left',
                    xpad: 0,
                    outlinewidth: 0,
                },
            };

            const layout = {
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(255,255,255,0.08)',
                font: { family: 'Inter, sans-serif', color: '#9898b8', size: 10 },
                xaxis: {
                    title: { text: 'Targets', font: { size: 12, color: '#9898b8' }, standoff: 5 },
                    showgrid: false,
                    side: 'top',
                    automargin: true,
                    fixedrange: true,
                },
                yaxis: {
                    autorange: 'reversed',
                    title: { text: 'Compounds', font: { size: 12, color: '#9898b8' }, standoff: 5 },
                    showgrid: false,
                    automargin: true,
                    fixedrange: true,
                },
                margin: { l: 85, r: 90, t: 60, b: 30 },
                dragmode: false,
                annotations: [
                    {
                        text: 'Selectivity',
                        font: { family: 'Inter, sans-serif', size: 11, color: '#9898b8' },
                        xref: 'paper',
                        yref: 'paper',
                        x: 1.08,
                        y: 0.95,
                        yshift: 2,
                        xanchor: 'center',
                        xshift: 9,
                        yanchor: 'bottom',
                        showarrow: false,
                    }
                ],
            };

            Plotly.newPlot('demoHeatmapChart', [trace], layout, {
                responsive: true,
                displayModeBar: false,
                scrollZoom: false,
                doubleClick: false,
            });
        }

        function initDemoDistributionChart() {
            const chartEl = document.getElementById('demoDistributionChart');
            if (!chartEl) return;

            const stats = [
                { target: 'MTOR', max: 1.95, median: 0.85, min: 0.40 },
                { target: 'BRAF', max: 1.92, median: 0.75, min: 0.35 },
                { target: 'ALK', max: 1.90, median: 1.10, min: 0.45 },
                { target: 'ERBB2', max: 1.88, median: 0.20, min: -0.60 },
                { target: 'EGFR', max: 1.85, median: 0.30, min: -0.50 },
                { target: 'KDR', max: 1.85, median: -0.20, min: -0.70 },
                { target: 'CDK4', max: 1.82, median: 0.65, min: 0.30 },
                { target: 'JAK2', max: 1.80, median: 0.55, min: 0.25 },
                { target: 'PIK3CA', max: 1.75, median: 0.10, min: -0.45 },
                { target: 'MET', max: 1.75, median: -0.25, min: -0.65 }
            ];

            const x = stats.map((_, i) => i);
            const hoverTemplate = '<b>%{customdata[3]}</b><br>Max: %{customdata[0]:.2f}<br>Median: %{customdata[1]:.2f}<br>Min: %{customdata[2]:.2f}<extra></extra>';
            const customData = stats.map(s => [s.max, s.median, s.min, s.target]);

            const minBases = [];
            const minLens = [];
            const medBases = [];
            const medLens = [];
            const maxBases = [];
            const maxLens = [];

            stats.forEach(s => {
                const mn = s.min;
                const md = s.median;
                const mx = s.max;

                // Min Segment (Green)
                if (mn >= 0) {
                    minBases.push(0);
                    minLens.push(mn);
                } else {
                    minBases.push(mn);
                    const minTop = Math.min(md, 0);
                    minLens.push(minTop - mn);
                }

                // Median Segment (Blue)
                if (md >= 0) {
                    const medBase = Math.max(0, mn);
                    medBases.push(medBase);
                    medLens.push(md - medBase);
                } else {
                    medBases.push(md);
                    const medTop = Math.min(mx, 0);
                    medLens.push(medTop - md);
                }

                // Max Segment (Purple)
                if (mx >= 0) {
                    const maxBase = Math.max(0, md);
                    maxBases.push(maxBase);
                    maxLens.push(mx - maxBase);
                } else {
                    maxBases.push(mx);
                    maxLens.push(0 - mx);
                }
            });

            const traceMax = {
                x: x,
                y: maxLens,
                base: maxBases,
                name: 'Max',
                type: 'bar',
                marker: { color: 'rgba(132, 94, 247, 0.8)' },
                width: 0.75,
                hovertemplate: hoverTemplate,
                customdata: customData,
            };

            const traceMedian = {
                x: x,
                y: medLens,
                base: medBases,
                name: 'Median',
                type: 'bar',
                marker: { color: 'rgba(77, 171, 247, 0.85)' },
                width: 0.75,
                hovertemplate: hoverTemplate,
                customdata: customData,
            };

            const traceMin = {
                x: x,
                y: minLens,
                base: minBases,
                name: 'Min',
                type: 'bar',
                marker: { color: 'rgba(56, 217, 169, 0.95)' },
                width: 0.75,
                hovertemplate: hoverTemplate,
                customdata: customData,
            };

            const layout = {
                barmode: 'overlay',
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0.15)',
                font: { family: 'Inter, sans-serif', color: '#9898b8' },
                xaxis: {
                    title: { text: 'Targets', font: { size: 12, color: '#9898b8' }, standoff: 15 },
                    tickvals: stats.map((_, i) => i),
                    ticktext: stats.map(s => s.target),
                    gridcolor: 'rgba(120, 120, 255, 0.08)',
                    range: [-0.85, stats.length - 0.15],
                    automargin: true,
                    fixedrange: true,
                },
                yaxis: {
                    title: { text: 'Selectivity Score', font: { size: 12, color: '#9898b8' }, standoff: 5 },
                    gridcolor: 'rgba(120, 120, 255, 0.08)',
                    zeroline: true,
                    zerolinecolor: 'rgba(255, 255, 255, 0.45)',
                    zerolinewidth: 2,
                    fixedrange: true,
                },
                legend: {
                    font: { size: 10 },
                    bgcolor: 'rgba(0,0,0,0.4)',
                    bordercolor: 'rgba(120,120,255,0.15)',
                    borderwidth: 1,
                    x: 0.98,
                    y: 0.98,
                    xanchor: 'right',
                    yanchor: 'top',
                },
                margin: { l: 50, r: 25, t: 20, b: 65 },
                hovermode: 'closest',
            };

            Plotly.newPlot('demoDistributionChart', [traceMax, traceMedian, traceMin], layout, {
                responsive: true,
                displayModeBar: false,
                scrollZoom: false,
                doubleClick: false,
            });
        }

        // ─── Demo History Chart ───
        function updateDemoHistoryChart(ftol = 0.0025) {
            const chartEl = document.getElementById('demoHistoryChart');
            if (!chartEl) return;

            // F-Tol controls convergence termination: lower ftol -> more generations, higher selectivity, lower cost
            const normFtol = Math.min(1, Math.max(0, (ftol - 0.0005) / (0.0100 - 0.0005)));
            const maxG = Math.round(280 - normFtol * 165);

            const generations = [];
            const selectivityData = [];
            const costData = [];

            for (let g = 1; g <= maxG; g++) {
                generations.push(g);
                const selProgress = 1 - Math.exp(-g / 40);
                const selNoise = Math.sin(g * 0.3) * 0.008 * (1 - selProgress);
                selectivityData.push(0.4 + 1.15 * selProgress + selNoise);
                const costProgress = 1 - Math.exp(-g / 45);
                const costNoise = Math.cos(g * 0.25) * 25 * (1 - costProgress);
                costData.push(5200 - 3900 * costProgress + costNoise);
            }

            const traceSel = {
                x: generations,
                y: selectivityData,
                name: 'Selectivity',
                mode: 'lines+markers',
                line: { color: '#12f3b9', width: 2 },
                marker: { size: 3 },
                hoverinfo: 'none',
            };

            const traceCost = {
                x: generations,
                y: costData,
                name: 'Cost',
                mode: 'lines+markers',
                line: { color: '#9d7cff', width: 2 },
                marker: { size: 3 },
                yaxis: 'y2',
                hoverinfo: 'none',
            };

            const layout = {
                margin: { t: 20, r: 90, l: 60, b: 65 },
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0.15)',
                font: { color: '#9898b8', family: 'Inter, sans-serif' },
                xaxis: {
                    title: { text: 'Generation', font: { size: 12, color: '#9898b8' }, standoff: 15 },
                    gridcolor: 'rgba(120, 120, 255, 0.08)',
                    automargin: true,
                    fixedrange: true,
                    range: [0, 300],
                    tickmode: 'array',
                    tickvals: [0, 50, 100, 150, 200, 250],
                },
                yaxis: {
                    title: { text: 'Best Selectivity Score', font: { size: 12, color: '#12f3b9' }, standoff: 15 },
                    gridcolor: 'rgba(120, 120, 255, 0.08)',
                    color: '#12f3b9',
                    automargin: true,
                    fixedrange: true,
                    range: [0.3, 1.7],
                },
                yaxis2: {
                    title: { text: 'Lowest Cost (USD)', font: { size: 12, color: '#9d7cff' }, standoff: 15 },
                    overlaying: 'y',
                    side: 'right',
                    color: '#9d7cff',
                    gridcolor: 'rgba(0,0,0,0)',
                    tickformat: '$,.0f',
                    automargin: true,
                    fixedrange: true,
                    range: [1000, 5500],
                },
                showlegend: true,
                legend: {
                    font: { size: 10 },
                    bgcolor: 'rgba(0,0,0,0.4)',
                    bordercolor: 'rgba(120,120,255,0.15)',
                    borderwidth: 1,
                    x: 0,
                    y: 1.1,
                    orientation: 'h',
                },
                hovermode: false,
            };

            Plotly.react('demoHistoryChart', [traceSel, traceCost], layout, {
                responsive: true,
                displayModeBar: false,
                scrollZoom: false,
                doubleClick: false,
            });
        }

        function initDemoHistoryChart() {
            updateDemoHistoryChart(0.0025);
        }

        // ─── Maximum Price Limit Demo Charts ───
        function initDemoBudgetUnconstrainedChart() {
            const chartEl = document.getElementById('demoBudgetUnconstrainedChart');
            if (!chartEl) return;

            const rawPoints = [
                { x: 0.68, y: 310 },
                { x: 0.74, y: 380 },
                { x: 0.82, y: 460 },
                { x: 0.91, y: 550 },
                { x: 1.02, y: 680 },
                { x: 1.12, y: 840 },
                { x: 1.22, y: 1050 },
                { x: 1.31, y: 1300 },
                { x: 1.40, y: 1620 },
                { x: 1.48, y: 1980 },
                { x: 1.55, y: 2400 },
                { x: 1.62, y: 2900 },
                { x: 1.67, y: 3450 },
                { x: 1.71, y: 4100 },
                { x: 1.74, y: 4800 },
            ];

            const x = rawPoints.map(p => p.x);
            const y = rawPoints.map(p => p.y);

            const traceAll = {
                x: x,
                y: y,
                mode: 'markers',
                type: 'scatter',
                name: 'Pareto Solutions',
                marker: {
                    size: 10,
                    color: 'rgba(56, 217, 169, 0.85)',
                    symbol: 'circle',
                    line: { width: 1.5, color: '#12f3b9' },
                },
                hovertemplate: 'Selectivity: %{x:.2f}<br>Cost: $%{y:,.0f}<extra></extra>',
                hoverlabel: {
                    bgcolor: 'rgba(56, 217, 169, 0.95)',
                    font: { color: '#070712', size: 12, family: 'Inter, sans-serif' },
                    bordercolor: 'transparent',
                },
            };

            const layout = {
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0.15)',
                font: { family: 'Inter, sans-serif', color: '#9898b8' },
                xaxis: {
                    title: { text: 'Selectivity Score', font: { size: 11, color: '#9898b8' }, standoff: 12 },
                    gridcolor: 'rgba(120, 120, 255, 0.08)',
                    zerolinecolor: 'rgba(120, 120, 255, 0.12)',
                    automargin: true,
                    fixedrange: true,
                    range: [0.6, 1.82],
                },
                yaxis: {
                    title: { text: 'Total Library Cost (USD)', font: { size: 11, color: '#9898b8' }, standoff: 12 },
                    gridcolor: 'rgba(120, 120, 255, 0.08)',
                    zerolinecolor: 'rgba(120, 120, 255, 0.12)',
                    tickformat: '$,.0f',
                    fixedrange: true,
                    range: [0, 5200],
                },
                legend: {
                    font: { size: 10 },
                    bgcolor: 'rgba(0,0,0,0.4)',
                    bordercolor: 'rgba(120,120,255,0.15)',
                    borderwidth: 1,
                    x: 0.02,
                    y: 0.98,
                    xanchor: 'left',
                    yanchor: 'top',
                },
                margin: { l: 65, r: 20, t: 15, b: 50 },
            };

            Plotly.newPlot('demoBudgetUnconstrainedChart', [traceAll], layout, {
                responsive: true,
                displayModeBar: false,
                scrollZoom: false,
                doubleClick: false,
            });
        }

        function initDemoBudgetFilteredChart() {
            const chartEl = document.getElementById('demoBudgetFilteredChart');
            if (!chartEl) return;

            const allPoints = [
                { x: 0.68, y: 310 },
                { x: 0.74, y: 380 },
                { x: 0.82, y: 460 },
                { x: 0.91, y: 550 },
                { x: 1.02, y: 680 },
                { x: 1.12, y: 840 },
                { x: 1.22, y: 1050 },
                { x: 1.31, y: 1300 },
                { x: 1.40, y: 1620 },
                { x: 1.48, y: 1980 },
                { x: 1.55, y: 2400 },
                { x: 1.62, y: 2900 },
                { x: 1.67, y: 3450 },
                { x: 1.71, y: 4100 },
                { x: 1.74, y: 4800 },
            ];

            const limit = 1000;
            const validPoints = allPoints.filter(p => p.y <= limit);
            const overPoints = allPoints.filter(p => p.y > limit);

            const traceValid = {
                x: validPoints.map(p => p.x),
                y: validPoints.map(p => p.y),
                mode: 'markers',
                type: 'scatter',
                name: 'Within Budget',
                marker: {
                    size: 10,
                    color: 'rgba(56, 217, 169, 0.85)',
                    symbol: 'circle',
                    line: { width: 1.5, color: '#12f3b9' },
                },
                hovertemplate: 'Selectivity: %{x:.2f}<br>Cost: $%{y:,.0f}<extra></extra>',
                hoverlabel: {
                    bgcolor: 'rgba(56, 217, 169, 0.95)',
                    font: { color: '#070712', size: 12, family: 'Inter, sans-serif' },
                    bordercolor: 'transparent',
                },
            };

            const traceOver = {
                x: overPoints.map(p => p.x),
                y: overPoints.map(p => p.y),
                mode: 'markers',
                type: 'scatter',
                name: 'Exceeds Limit',
                marker: {
                    size: 9,
                    color: 'rgba(100, 100, 130, 0.35)',
                    symbol: 'circle',
                    line: { width: 1.5, color: 'rgba(140, 140, 170, 0.5)' },
                },
                hovertemplate: 'Selectivity: %{x:.2f}<br>Cost: $%{y:,.0f}<br><b style="color:#ff6b6b">Exceeds $1,000 Limit</b><extra></extra>',
                hoverlabel: {
                    bgcolor: 'rgba(25, 25, 38, 0.95)',
                    font: { color: '#c0c0d8', size: 12, family: 'Inter, sans-serif' },
                    bordercolor: 'rgba(255, 107, 107, 0.4)',
                },
            };

            const traceLimitLine = {
                x: [0.6, 1.82],
                y: [limit, limit],
                mode: 'lines',
                type: 'scatter',
                name: 'Limit ($1,000)',
                line: { color: 'rgba(255, 94, 176, 0.85)', width: 1.75, dash: 'dash' },
                hoverinfo: 'skip',
            };

            const layout = {
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0.15)',
                font: { family: 'Inter, sans-serif', color: '#9898b8' },
                xaxis: {
                    title: { text: 'Selectivity Score', font: { size: 11, color: '#9898b8' }, standoff: 12 },
                    gridcolor: 'rgba(120, 120, 255, 0.08)',
                    zerolinecolor: 'rgba(120, 120, 255, 0.12)',
                    automargin: true,
                    fixedrange: true,
                    range: [0.6, 1.82],
                },
                yaxis: {
                    title: { text: 'Total Library Cost (USD)', font: { size: 11, color: '#9898b8' }, standoff: 12 },
                    gridcolor: 'rgba(120, 120, 255, 0.08)',
                    zerolinecolor: 'rgba(120, 120, 255, 0.12)',
                    tickformat: '$,.0f',
                    fixedrange: true,
                    range: [0, 5200],
                },
                legend: {
                    font: { size: 10 },
                    bgcolor: 'rgba(0,0,0,0.4)',
                    bordercolor: 'rgba(120,120,255,0.15)',
                    borderwidth: 1,
                    x: 0.02,
                    y: 0.98,
                    xanchor: 'left',
                    yanchor: 'top',
                },
                margin: { l: 65, r: 20, t: 15, b: 50 },
            };

            Plotly.newPlot('demoBudgetFilteredChart', [traceValid, traceOver, traceLimitLine], layout, {
                responsive: true,
                displayModeBar: false,
                scrollZoom: false,
                doubleClick: false,
            });
        }

        // Initialize charts lazily when they scroll into view
        const chartInitMap = {
            'demoHistoryChart': { init: initDemoHistoryChart, done: false },
            'demoBudgetUnconstrainedChart': { init: initDemoBudgetUnconstrainedChart, done: false },
            'demoBudgetFilteredChart': { init: initDemoBudgetFilteredChart, done: false },
            'demoParetoChart': { init: initDemoParetoChart, done: false },
            'demoHeatmapChart': { init: initDemoHeatmapChart, done: false },
            'demoDistributionChart': { init: initDemoDistributionChart, done: false },
        };

        function lazyInitCharts() {
            const chartObserver = new IntersectionObserver((entries) => {
                entries.forEach(entry => {
                    if (entry.isIntersecting) {
                        const id = entry.target.id;
                        const chart = chartInitMap[id];
                        if (chart && !chart.done) {
                            chart.init();
                            chart.done = true;
                        }
                        chartObserver.unobserve(entry.target);
                    }
                });
            }, { rootMargin: '200px 0px' });

            Object.keys(chartInitMap).forEach(id => {
                const el = document.getElementById(id);
                if (el) chartObserver.observe(el);
            });
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', lazyInitCharts);
        } else {
            lazyInitCharts();
        }

        // Debounced resize handler
        let resizeTimer;
        window.addEventListener('resize', () => {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(() => {
                Object.keys(chartInitMap).forEach(id => {
                    if (chartInitMap[id].done) {
                        Plotly.Plots.resize(id);
                    }
                });
            }, 150);
        });
