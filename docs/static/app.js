/* Brickonomy client: Chart.js charts + refresh-progress polling. */
(function () {
  "use strict";

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // Static-export mode: pages are served from GitHub Pages (or any file host);
  // API endpoints become pre-rendered .json files under the site base path.
  const IS_STATIC = document.body.dataset.static === "1";
  // Relative prefix for the current page ('' at the site root, '../' one level
  // down), so the export works at any mount point.
  const BASE = document.body.dataset.base || "";
  const apiURL = (path) => (IS_STATIC ? `${BASE}${path.replace(/^\//, "")}.json` : path);

  const CHART_DEFAULTS = {
    color: css("--muted") || "#66718f",
    borderColor: css("--grid") || "rgba(124,160,255,0.10)",
  };
  if (window.Chart) {
    Chart.defaults.color = CHART_DEFAULTS.color;
    Chart.defaults.borderColor = CHART_DEFAULTS.borderColor;
    Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  }

  const SOURCE_STYLE = {
    blended:   { color: css("--s1") || "#3987e5", width: 2.5, label: "Blended" },
    bricklink: { color: css("--s2") || "#d95926", width: 1.5, label: "BrickLink" },
    ebay:      { color: css("--s3") || "#199e70", width: 1.5, label: "eBay" },
    brickowl:  { color: css("--s4") || "#c98500", width: 1.5, label: "BrickOwl" },
  };

  // ── set detail: history + forecast ─────────────────────────────────────
  const historyCanvas = document.getElementById("historyChart");
  if (historyCanvas && window.Chart) {
    const itemId = historyCanvas.dataset.item;
    const retiredYear = historyCanvas.dataset.retired;

    // Vertical marker at the estimated retirement date.
    const retirementMarker = {
      id: "retirementMarker",
      afterDatasetsDraw(chart) {
        if (!retiredYear) return;
        const x = chart.scales.x.getPixelForValue(new Date(`${retiredYear}-06-30`));
        const { top, bottom } = chart.chartArea;
        if (x < chart.chartArea.left || x > chart.chartArea.right) return;
        const c = chart.ctx;
        c.save();
        c.setLineDash([4, 4]);
        c.strokeStyle = css("--s2") || "#d95926";
        c.lineWidth = 1.5;
        c.beginPath(); c.moveTo(x, top); c.lineTo(x, bottom); c.stroke();
        c.setLineDash([]);
        c.fillStyle = css("--s2") || "#d95926";
        c.font = "11px system-ui, sans-serif";
        c.textAlign = "center";
        c.fillText("retired", x, top + 11);
        c.restore();
      },
    };

    fetch(apiURL(`/api/sets/${itemId}/history`))
      .then((r) => r.json())
      .then((data) => {
        const datasets = [];
        for (const [source, style] of Object.entries(SOURCE_STYLE)) {
          const pts = (data.series[source] || []).map((p) => ({ x: p.t, y: p.v }));
          if (!pts.length) continue;
          datasets.push({
            label: style.label, data: pts,
            borderColor: style.color, backgroundColor: style.color,
            borderWidth: style.width, pointRadius: pts.length < 15 ? 3 : 0,
            tension: 0.25, spanGaps: true,
          });
        }
        if (data.forecast && data.forecast.length > 1) {
          const f = data.forecast;
          datasets.push({
            label: "Forecast", data: f.map((p) => ({ x: p.t, y: p.v })),
            borderColor: SOURCE_STYLE.blended.color, borderDash: [6, 5],
            borderWidth: 2, pointRadius: 3, tension: 0, fill: false,
          });
          datasets.push({
            label: "Forecast high", data: f.map((p) => ({ x: p.t, y: p.hi })),
            borderColor: "transparent", pointRadius: 0, fill: "+1",
            backgroundColor: "rgba(57,135,229,0.14)", tension: 0,
          });
          datasets.push({
            label: "Forecast low", data: f.map((p) => ({ x: p.t, y: p.lo })),
            borderColor: "transparent", pointRadius: 0, fill: false, tension: 0,
          });
        }
        const chart = new Chart(historyCanvas, {
          type: "line",
          data: { datasets },
          plugins: [retirementMarker],
          options: {
            responsive: true, maintainAspectRatio: false,
            interaction: { mode: "nearest", axis: "x", intersect: false },
            plugins: {
              legend: { display: false },
              tooltip: {
                filter: (item) => !item.dataset.label.startsWith("Forecast "),
                callbacks: {
                  label: (item) =>
                    ` ${item.dataset.label}: ${item.parsed.y.toLocaleString()} ${data.currency}`,
                },
              },
            },
            scales: {
              x: { type: "time", time: { unit: "month" }, grid: { display: false } },
              y: { ticks: { callback: (v) => v.toLocaleString() } },
            },
          },
        });

        // Range buttons (1Y / 3Y / All) clamp the x axis.
        document.querySelectorAll(".rangebtn").forEach((btn) => {
          btn.addEventListener("click", () => {
            document.querySelectorAll(".rangebtn").forEach((b) => b.classList.remove("active"));
            btn.classList.add("active");
            const days = Number(btn.dataset.range);
            if (!days) {
              delete chart.options.scales.x.min;
            } else {
              const from = new Date();
              from.setDate(from.getDate() - days);
              chart.options.scales.x.min = from.toISOString().slice(0, 10);
            }
            chart.update();
          });
        });
      })
      .catch(() => { historyCanvas.parentElement.textContent = "Could not load price history."; });
  }

  // ── portfolio: value over time ─────────────────────────────────────────
  const pfCanvas = document.getElementById("portfolioChart");
  if (pfCanvas && window.Chart) {
    fetch(apiURL("/api/portfolio/history"))
      .then((r) => r.json())
      .then((data) => {
        const pts = (data.series || []).map((p) => ({ x: p.t, y: p.v }));
        if (!pts.length) {
          pfCanvas.parentElement.textContent = "No history yet — run a scan to record the first snapshot.";
          return;
        }
        new Chart(pfCanvas, {
          type: "line",
          data: {
            datasets: [{
              label: "Portfolio value", data: pts,
              borderColor: SOURCE_STYLE.blended.color, borderWidth: 2.5,
              backgroundColor: "rgba(57,135,229,0.14)", fill: true,
              pointRadius: pts.length < 15 ? 3 : 0, tension: 0.25,
            }],
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            plugins: {
              legend: { display: false },
              tooltip: { callbacks: { label: (item) =>
                ` ${item.parsed.y.toLocaleString()} ${data.currency}` } },
            },
            scales: {
              x: { type: "time", time: { unit: "month" }, grid: { display: false } },
              y: { ticks: { callback: (v) => v.toLocaleString() } },
            },
          },
        });
      })
      .catch(() => { pfCanvas.parentElement.textContent = "Could not load portfolio history."; });
  }

  // ── header search: instant catalog lookup, works statically too ───────
  const searchInput = document.getElementById("siteSearch");
  const searchResults = document.getElementById("siteSearchResults");
  if (searchInput && searchResults) {
    const pageURL = (id) => (IS_STATIC ? `${BASE}sets/${id}.html` : `/sets/${id}`);
    let index = null, active = -1;

    const loadIndex = () => {
      if (index) return Promise.resolve(index);
      return fetch(apiURL("/api/index"))
        .then((r) => r.json())
        .then((d) => { index = d.items || []; return index; })
        .catch(() => (index = []));
    };

    const render = (matches) => {
      active = -1;
      if (!matches.length) { searchResults.hidden = true; return; }
      searchResults.innerHTML = matches
        .map((i) => `<a href="${pageURL(i.id)}"><b>${i.id}</b> ${i.name}
          <span>${i.type === "M" ? "minifig" : [i.theme, i.year].filter(Boolean).join(" · ")}</span></a>`)
        .join("");
      searchResults.hidden = false;
    };

    const search = () => {
      const q = searchInput.value.trim().toLowerCase();
      if (q.length < 2) { searchResults.hidden = true; return; }
      loadIndex().then((items) => {
        const starts = [], contains = [];
        for (const i of items) {
          const id = i.id.toLowerCase(), name = (i.name || "").toLowerCase();
          if (id.startsWith(q)) starts.push(i);
          else if (id.includes(q) || name.includes(q)) contains.push(i);
          if (starts.length >= 8) break;
        }
        render(starts.concat(contains).slice(0, 8));
      });
    };

    searchInput.addEventListener("input", search);
    searchInput.addEventListener("focus", () => { loadIndex(); search(); });
    searchInput.addEventListener("keydown", (e) => {
      const links = [...searchResults.querySelectorAll("a")];
      if (e.key === "Escape") { searchResults.hidden = true; searchInput.blur(); }
      if (!links.length) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        active = (active + (e.key === "ArrowDown" ? 1 : -1) + links.length) % links.length;
        links.forEach((l, i) => l.classList.toggle("on", i === active));
      } else if (e.key === "Enter") {
        e.preventDefault();
        (links[active] || links[0]).click();
      }
    });
    document.addEventListener("click", (e) => {
      if (!searchResults.contains(e.target) && e.target !== searchInput) {
        searchResults.hidden = true;
      }
    });
  }

  // ── client-side table filter (static pages have no server search) ──────
  document.querySelectorAll("input[data-filter-table]").forEach((input) => {
    const table = document.getElementById(input.dataset.filterTable);
    if (!table) return;
    input.addEventListener("input", () => {
      const needle = input.value.trim().toLowerCase();
      table.querySelectorAll("tr").forEach((tr, i) => {
        if (i === 0) return; // header
        tr.style.display = !needle || tr.textContent.toLowerCase().includes(needle) ? "" : "none";
      });
    });
  });

  // ── refresh page: live progress polling ────────────────────────────────
  const scanStatus = document.getElementById("scanStatus");
  if (scanStatus) {
    const poll = () => {
      fetch("/api/refresh/status")
        .then((r) => r.json())
        .then((s) => {
          const bar = document.getElementById("scanBar");
          const log = document.getElementById("scanLog");
          if (bar && s.total) bar.style.width = `${Math.round((s.done / s.total) * 100)}%`;
          if (log) { log.textContent = s.log.join("\n"); log.scrollTop = log.scrollHeight; }
          const done = document.getElementById("scanDone");
          const total = document.getElementById("scanTotal");
          const current = document.getElementById("scanCurrent");
          if (done) done.textContent = s.done;
          if (total) total.textContent = s.total;
          if (current) current.innerHTML = s.current_item ? `— currently: <b>${s.current_item}</b>` : "";
          if (s.running) setTimeout(poll, 2000);
          else if (scanStatus.dataset.running === "1") window.location.reload();
        })
        .catch(() => setTimeout(poll, 5000));
    };
    if (scanStatus.dataset.running === "1") setTimeout(poll, 2000);
  }
})();
