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
        new Chart(historyCanvas, {
          type: "line",
          data: { datasets },
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
