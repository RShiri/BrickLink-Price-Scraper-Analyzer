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

        // Used-condition blended value, so both conditions read on one axis.
        const usedPts = ((data.series_used || {}).blended || [])
          .map((p) => ({ x: p.t, y: p.v }));
        if (usedPts.length) {
          datasets.push({
            label: "Used", data: usedPts,
            borderColor: css("--s5") || "#9085e9",
            backgroundColor: css("--s5") || "#9085e9",
            borderWidth: 2, pointRadius: usedPts.length < 15 ? 3 : 0,
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
    let index = null, active = -1;
    const loadIndex = () => loadCatalog().then((items) => (index = items));

    const render = (matches) => {
      active = -1;
      if (!matches.length) { searchResults.hidden = true; return; }
      searchResults.innerHTML = matches
        .map((i) => `<a href="${itemURL(i)}"><b>${i.id}</b> ${i.name}
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

  // ── catalog helpers shared by the browser and the lite set page ───────
  let catalogPromise = null;
  const loadCatalog = () => {
    if (!catalogPromise) {
      catalogPromise = fetch(apiURL("/api/index"))
        .then((r) => r.json())
        .then((d) => {
          // Compact wire format: rows of [id, name, themeIndex, year, parts, type, priced]
          const themes = d.themes || [];
          return (d.rows || []).map((r) => ({
            id: r[0], name: r[1], theme: r[2] >= 0 ? themes[r[2]] : "",
            year: r[3] || null, parts: r[4] || null, type: r[5], p: r[6],
          }));
        })
        .catch(() => []);
    }
    return catalogPromise;
  };
  const itemURL = (i) =>
    i.p
      ? (IS_STATIC ? `${BASE}sets/${i.id}.html` : `/sets/${i.id}`)
      : (IS_STATIC ? `${BASE}set.html?id=${encodeURIComponent(i.id)}`
                   : `/sets/${i.id}`);
  const setImg = (id, type) =>
    type === "M" || /^[a-z]/i.test(id)
      ? `https://img.bricklink.com/ItemImage/MN/0/${id}.png`
      : `https://img.bricklink.com/ItemImage/SN/0/${id.includes("-") ? id : id + "-1"}.png`;
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ── full-catalog browser (static Sets page over ~23k rows) ────────────
  const catalogTable = document.getElementById("catalogTable");
  if (catalogTable) {
    const qEl = document.getElementById("catalogSearch");
    const themeEl = document.getElementById("catalogTheme");
    const sortEl = document.getElementById("catalogSort");
    const pricedEl = document.getElementById("catalogPriced");
    const countEl = document.getElementById("catalogCount");
    const moreBtn = document.getElementById("catalogMore");
    const body = catalogTable.querySelector("tbody");
    const PAGE = 100;
    let all = [], shown = 0, matches = [];

    const sorters = {
      year: (a, b) => (b.year || 0) - (a.year || 0) || a.id.localeCompare(b.id),
      "year-asc": (a, b) => (a.year || 9999) - (b.year || 9999) || a.id.localeCompare(b.id),
      id: (a, b) => a.id.localeCompare(b.id, undefined, { numeric: true }),
      parts: (a, b) => (b.parts || 0) - (a.parts || 0),
    };

    const rowHTML = (i) => `<tr>
      <td><img class="thumb" src="${setImg(i.id, i.type)}" alt="" loading="lazy"
               onerror="this.style.visibility='hidden'"><a href="${itemURL(i)}"><b>${esc(i.id)}</b> ${esc(i.name)}</a>
          ${i.type === "M" ? '<span style="color:var(--muted)"> (minifig)</span>' : ""}</td>
      <td>${esc(i.theme) || "—"}</td>
      <td class="num">${i.year || "—"}</td>
      <td class="num">${i.parts ? i.parts.toLocaleString() : "—"}</td>
      <td>${i.p ? '<span class="chip on">priced</span>'
                : '<span class="chip" style="color:var(--muted)">not scanned</span>'}</td>
    </tr>`;

    const draw = (append) => {
      const slice = matches.slice(append ? shown : 0, (append ? shown : 0) + PAGE);
      if (!append) { body.innerHTML = ""; shown = 0; }
      body.insertAdjacentHTML("beforeend", slice.map(rowHTML).join(""));
      shown += slice.length;
      countEl.textContent = `${matches.length.toLocaleString()} matching item${
        matches.length === 1 ? "" : "s"} — showing ${shown.toLocaleString()}`;
      moreBtn.hidden = shown >= matches.length;
    };

    const apply = () => {
      const q = qEl.value.trim().toLowerCase();
      const theme = themeEl.value;
      const pricedOnly = pricedEl.checked;
      matches = all.filter((i) => {
        if (pricedOnly && !i.p) return false;
        if (theme && i.theme !== theme) return false;
        if (!q) return true;
        return i.id.toLowerCase().includes(q) || (i.name || "").toLowerCase().includes(q);
      });
      matches.sort(sorters[sortEl.value] || sorters.year);
      draw(false);
    };

    loadCatalog().then((items) => {
      all = items;
      const themes = [...new Set(items.map((i) => i.theme).filter(Boolean))].sort();
      themeEl.insertAdjacentHTML("beforeend",
        themes.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join(""));
      // Deep-link support: /sets?q=falcon keeps working in the static build.
      const params = new URLSearchParams(location.search);
      if (params.get("q")) qEl.value = params.get("q");
      if (params.get("theme")) themeEl.value = params.get("theme");
      apply();
    });

    [qEl, themeEl, sortEl, pricedEl].forEach((el) =>
      el.addEventListener("input", apply));
    moreBtn.addEventListener("click", () => draw(true));
  }

  // ── lite set page for catalog items without scraped prices ────────────
  const lite = document.getElementById("liteSet");
  if (lite) {
    const id = new URLSearchParams(location.search).get("id") || "";
    document.getElementById("liteId").textContent = id || "<set>";
    const blNum = id.includes("-") ? id : `${id}-1`;
    document.getElementById("liteBL").href =
      `https://www.bricklink.com/v2/catalog/catalogitem.page?S=${encodeURIComponent(blNum)}`;
    document.getElementById("liteBE").href =
      `https://www.brickeconomy.com/set/${encodeURIComponent(blNum)}`;
    document.getElementById("liteEbay").href =
      `https://www.ebay.com/sch/i.html?_nkw=lego+${encodeURIComponent(id)}`;

    loadCatalog().then((items) => {
      const item = items.find((i) => i.id === id);
      if (!item) {
        document.getElementById("liteName").textContent = id || "Unknown set";
        document.getElementById("liteMeta").textContent =
          "Not in the catalog index. Import it with python -m brickonomy.rebrickable.";
        return;
      }
      document.title = `${item.id} ${item.name} · Brickonomy`;
      document.getElementById("liteTheme").textContent = item.theme || "Catalog";
      document.getElementById("liteName").innerHTML =
        `${esc(item.name)} <span style="color:var(--muted);font-weight:400">· ${esc(item.id)}</span>`;
      document.getElementById("liteMeta").textContent = [
        item.year ? `Released ${item.year}` : null,
        item.parts ? `${item.parts.toLocaleString()} parts` : null,
      ].filter(Boolean).join(" · ");

      const related = items
        .filter((i) => i.theme && i.theme === item.theme && i.id !== item.id)
        .sort((a, b) => Math.abs((a.year || 0) - (item.year || 0)) -
                        Math.abs((b.year || 0) - (item.year || 0)))
        .slice(0, 12);
      document.getElementById("liteRelated").innerHTML = related.map((i) => `
        <a class="relcard" href="${itemURL(i)}">
          <img src="${setImg(i.id, i.type)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'">
          <div class="relname"><b>${esc(i.id)}</b> ${esc((i.name || "").slice(0, 34))}</div>
          <div class="relmeta">${i.year || ""}${i.p ? " · priced" : ""}</div>
        </a>`).join("");
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

  // ── sortable tables ────────────────────────────────────────────────────
  // Any <table data-sortable> gets click-to-sort headers. Numeric cells carry
  // data-v="<raw number>" so sorting is never fooled by "₪3.23", "▲ 8%" or
  // "—"; anything else compares as text. Missing values sink to the bottom in
  // both directions, which is what you want when half a column is unscanned.
  const cellValue = (row, idx) => {
    const td = row.children[idx];
    if (!td) return null;
    if (td.hasAttribute("data-v")) {
      const raw = td.getAttribute("data-v");
      if (raw === "" || raw === null) return null;
      const n = parseFloat(raw);
      return Number.isNaN(n) ? raw.toLowerCase() : n;
    }
    const text = td.textContent.trim();
    if (!text || text === "—") return null;
    return text.toLowerCase();
  };

  const compare = (a, b) => {
    if (a === null && b === null) return 0;
    if (a === null) return 1;            // blanks last, always
    if (b === null) return -1;
    if (typeof a === "number" && typeof b === "number") return a - b;
    return String(a).localeCompare(String(b), undefined, { numeric: true });
  };

  window.brickonomySortRows = (rows, idx, dir) =>
    rows.sort((r1, r2) => {
      const c = compare(cellValue(r1, idx), cellValue(r2, idx));
      // Blanks stay last even when the direction flips.
      if (cellValue(r1, idx) === null || cellValue(r2, idx) === null) return c;
      return dir === "desc" ? -c : c;
    });

  document.querySelectorAll("table[data-sortable]").forEach((table) => {
    const head = table.tHead ? table.tHead.rows[0] : table.rows[0];
    const body = table.tBodies[0];
    if (!head || !body) return;

    [...head.cells].forEach((th, idx) => {
      if (th.dataset.nosort !== undefined) return;
      th.tabIndex = 0;
      th.dataset.sort = "";
      const run = () => {
        // First click on a numeric column sorts high→low; text sorts A→Z.
        const wasNumeric = body.rows[0] &&
          body.rows[0].children[idx] &&
          body.rows[0].children[idx].hasAttribute("data-v");
        const current = th.dataset.sort;
        const dir = current === "asc" ? "desc" : current === "desc" ? "asc"
                                                                   : (wasNumeric ? "desc" : "asc");
        [...head.cells].forEach((o) => { o.dataset.sort = ""; });
        th.dataset.sort = dir;
        window.brickonomySortRows([...body.rows], idx, dir)
          .forEach((tr) => body.appendChild(tr));
      };
      th.addEventListener("click", run);
      th.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); run(); }
      });
    });
  });

  // ── minifig breakdown: sort the tiles, and swap grid ⇄ table ───────────
  const figGrid = document.getElementById("figGrid");
  if (figGrid) {
    const figSort = document.getElementById("figSort");
    const figTable = document.getElementById("figTable");
    const viewBox = document.getElementById("figView");

    const sorters = {
      "value-desc": (a, b) => (+b.dataset.value) - (+a.dataset.value),
      "value-asc": (a, b) => (+a.dataset.value) - (+b.dataset.value),
      "name-asc": (a, b) => a.dataset.name.localeCompare(b.dataset.name),
      "qty-desc": (a, b) => (+b.dataset.qty) - (+a.dataset.qty),
    };
    const applySort = () => {
      const fn = sorters[figSort.value] || sorters["value-desc"];
      [...figGrid.children].sort(fn).forEach((el) => figGrid.appendChild(el));
      localStorage.setItem("figSort", figSort.value);
    };
    const saved = localStorage.getItem("figSort");
    if (saved && sorters[saved]) figSort.value = saved;
    figSort.addEventListener("change", applySort);
    applySort();

    const setView = (view) => {
      figGrid.hidden = view !== "grid";
      figTable.hidden = view !== "table";
      viewBox.querySelectorAll("[data-view]").forEach((b) =>
        b.classList.toggle("on", b.dataset.view === view));
      localStorage.setItem("figView", view);
    };
    viewBox.querySelectorAll("[data-view]").forEach((b) =>
      b.addEventListener("click", () => setView(b.dataset.view)));
    setView(localStorage.getItem("figView") === "table" ? "table" : "grid");
  }

  // ── auto-scan banner: wait for this item's scan, then show the prices ──
  const autoScan = document.getElementById("autoScan");
  if (autoScan) {
    const id = autoScan.dataset.item;
    const text = document.getElementById("autoScanText");
    let seen = false;                    // became visible in the job state
    const poll = () => {
      // Absolute path is safe: the banner only renders on the live server,
      // never in the static export.
      fetch("/api/refresh/status")
        .then((r) => r.json())
        .then((s) => {
          const queued = (s.queue || []).indexOf(id);
          const active = s.current_item === id;
          if (active || queued >= 0) {
            seen = true;
            if (text) {
              text.textContent = active
                ? "Fetching prices from BrickLink, eBay and BrickOwl…"
                : `Queued for a refresh, ${queued} ahead.`;
            }
            setTimeout(poll, 3000);
          } else if (seen || !s.running) {
            window.location.reload();    // our scan finished — show the result
          } else {
            setTimeout(poll, 3000);
          }
        })
        .catch(() => setTimeout(poll, 6000));
    };
    setTimeout(poll, 2500);
  }

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
