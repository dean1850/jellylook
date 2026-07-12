/* jellylook frontend — vanilla JS, no framework, no localStorage. */
(() => {
  "use strict";

  const state = {
    users: [],
    selected: new Set(),
    filter: "all",
    sort: "match",
    page: 1,
    pages: 0,
    scanning: false,
    tvRequestMode: "ask",
    seerrReachable: true,
    seerrStatusCache: new Map(), // "type:tmdb" -> status|null
  };

  const $ = (sel) => document.querySelector(sel);
  const grid = $("#grid");
  const pager = $("#pager");
  const statusLine = $("#status-line");
  const scanBtn = $("#btn-scan");

  const api = async (path, opts) => {
    const resp = await fetch(path, opts);
    if (!resp.ok) {
      let msg = `Request failed (${resp.status})`;
      try { msg = (await resp.json()).detail || msg; } catch (_) { /* keep */ }
      throw new Error(msg);
    }
    return resp.json();
  };

  const setStatus = (text, isError = false) => {
    statusLine.textContent = text || "";
    statusLine.classList.toggle("is-error", isError);
  };

  /* ---- users + settings bootstrap -------------------------------------- */
  async function init() {
    try {
      const [users, settings, status] = await Promise.all([
        api("/api/users"), api("/api/settings"), api("/api/status"),
      ]);
      state.users = users;
      state.tvRequestMode = settings.tv_request_mode || "ask";
      state.sort = settings.default_sort || "match";
      state.filter = settings.default_filter || "all";
      state.seerrReachable = !!status.seerr_reachable;
      $("#sort-select").value = state.sort;
      syncFilterButtons();
      const defaults = (settings.default_user_ids || "").split(",").filter(Boolean);
      users.forEach((u) => { if (defaults.includes(u.id)) state.selected.add(u.id); });
      renderUserChips();
      renderLastScan(status.last_scans);
      fillSettingsPanel(settings, status);
      await loadRecommendations();
      if (!grid.children.length) {
        setStatus("Pick who's watching and hit Scan.");
      }
    } catch (err) {
      setStatus(`Can't reach the jellylook API. ${err.message}`, true);
    }
  }

  function renderUserChips() {
    const wrap = $("#user-chips");
    wrap.innerHTML = "";
    if (!state.users.length) {
      wrap.innerHTML = '<span class="meta">No users found — check the history source in .env.</span>';
      updateScanButton();
      return;
    }
    state.users.forEach((u) => {
      const label = document.createElement("label");
      label.className = "chip" + (state.selected.has(u.id) ? " is-on" : "");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = state.selected.has(u.id);
      box.addEventListener("change", () => {
        box.checked ? state.selected.add(u.id) : state.selected.delete(u.id);
        label.classList.toggle("is-on", box.checked);
        updateScanButton();
      });
      label.append(box, document.createTextNode(u.name));
      wrap.appendChild(label);
    });
    updateScanButton();
  }

  function updateScanButton() {
    scanBtn.disabled = state.scanning || state.selected.size === 0;
  }

  function renderLastScan(scans) {
    const last = scans && scans[0];
    if (!last) { $("#last-scan-meta").textContent = ""; return; }
    const names = (last.source_users || "").split(",")
      .map((id) => (state.users.find((u) => u.id === id) || {}).name || null)
      .filter(Boolean).join(", ");
    $("#last-scan-meta").textContent =
      `last scan · ${relTime(last.created_at)}${names ? " · " + names : ""}`;
  }

  function relTime(iso) {
    const mins = Math.max(0, (Date.now() - Date.parse(iso)) / 60000);
    if (mins < 60) return `${Math.round(mins)}m ago`;
    if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
    return `${Math.round(mins / 1440)}d ago`;
  }

  /* ---- scan ------------------------------------------------------------- */
  scanBtn.addEventListener("click", async () => {
    if (state.scanning || !state.selected.size) return;
    state.scanning = true;
    updateScanButton();
    scanBtn.classList.add("is-scanning");
    setStatus("Scanning…");
    renderSkeletons();
    try {
      await api("/api/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_ids: [...state.selected] }),
      });
      await pollScan();
    } catch (err) {
      setStatus(err.message, true);
      grid.innerHTML = "";
    } finally {
      state.scanning = false;
      scanBtn.classList.remove("is-scanning");
      updateScanButton();
    }
  });

  async function pollScan() {
    let misses = 0;
    for (;;) {
      await sleep(1500);
      let st;
      try {
        st = await api("/api/scan/status");
        misses = 0;
      } catch (_) {
        // One flaky poll shouldn't abandon a scan that's still running.
        if (++misses >= 5) throw new Error("Lost contact with the jellylook API.");
        continue;
      }
      if (st.state === "running") {
        setStatus(`Scanning… ${st.detail || st.step || ""}`);
        continue;
      }
      if (st.state === "failed") {
        setStatus(st.error || "The scan failed.", true);
        grid.innerHTML = "";
        return;
      }
      if (st.state === "done") {
        state.page = 1;
        state.seerrStatusCache.clear();
        setStatus("");
        const status = await api("/api/status");
        state.seerrReachable = !!status.seerr_reachable;
        renderLastScan(status.last_scans);
        await loadRecommendations();
        return;
      }
      // idle/unknown — the server restarted mid-scan. Recover gracefully.
      setStatus("The scan was interrupted. Showing the last saved results.", true);
      await loadRecommendations();
      return;
    }
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function renderSkeletons() {
    grid.innerHTML = "";
    pager.innerHTML = "";
    for (let i = 0; i < 10; i++) {
      const sk = document.createElement("div");
      sk.className = "skeleton";
      grid.appendChild(sk);
    }
  }

  /* ---- recommendations ---------------------------------------------------- */
  async function loadRecommendations() {
    const params = new URLSearchParams({
      type: state.filter, sort: state.sort, page: state.page,
    });
    const data = await api(`/api/recommendations?${params}`);
    state.pages = data.pages;
    renderGrid(data.items);
    renderPager();
    if (data.total === 0 && data.scan_id) {
      setStatus("No recommendations match this filter.");
    }
    lazySeerrStatus(data.items);
  }

  function renderGrid(items) {
    grid.innerHTML = "";
    items.forEach((rec, i) => grid.appendChild(card(rec, i)));
  }

  function card(rec, i) {
    const el = document.createElement("article");
    el.className = "card";
    el.style.setProperty("--i", i);

    const posterWrap = document.createElement("div");
    posterWrap.className = "poster-wrap";
    if (rec.poster_url) {
      const img = document.createElement("img");
      img.src = rec.poster_url;
      img.alt = `${rec.title} poster`;
      img.loading = "lazy";
      posterWrap.appendChild(img);
    } else {
      const fb = document.createElement("div");
      fb.className = "poster-fallback";
      fb.textContent = rec.title;
      posterWrap.appendChild(fb);
    }
    if (rec.is_in_library) {
      const chip = document.createElement("span");
      chip.className = "lib-chip";
      chip.style.cssText = "position:absolute;top:8px;right:8px;background:var(--surface)";
      chip.textContent = "In library";
      posterWrap.appendChild(chip);
    } else if (rec.match_score != null) {
      const badge = document.createElement("span");
      badge.className = "match-badge";
      badge.style.setProperty("--pct", rec.match_score);
      badge.textContent = `${rec.match_score}%`;
      badge.title = `Match ${rec.match_score}% — the AI's similarity estimate`;
      posterWrap.appendChild(badge);
    }

    const body = document.createElement("div");
    body.className = "card-body";

    const title = document.createElement("h3");
    title.className = "card-title";
    title.innerHTML = `${escapeHtml(rec.title)} ` +
      (rec.year ? `<span class="year">’${String(rec.year).slice(-2)}</span>` : "");
    body.appendChild(title);

    const ratings = document.createElement("div");
    ratings.className = "rating-row";
    if (rec.imdb_rating != null) {
      const b = document.createElement("span");
      b.className = "imdb-badge";
      b.textContent = `★ ${rec.imdb_rating.toFixed(1)}`;
      ratings.appendChild(b);
    }
    if (rec.tmdb_rating != null) {
      const t = document.createElement("span");
      t.className = "tmdb-score";
      t.textContent = `TMDb ${rec.tmdb_rating.toFixed(1)}`;
      ratings.appendChild(t);
    }
    const type = document.createElement("span");
    type.className = "type-chip";
    type.textContent = rec.media_type === "tv" ? "tv" : "film";
    ratings.appendChild(type);
    body.appendChild(ratings);

    if (rec.because_of) {
      const because = document.createElement("p");
      because.className = "because";
      because.innerHTML = `Because you watched <strong>${escapeHtml(rec.because_of)}</strong>`;
      body.appendChild(because);
    }
    if (rec.reason) {
      const reason = document.createElement("p");
      reason.className = "reason";
      reason.textContent = rec.reason;
      body.appendChild(reason);
    }

    const actions = document.createElement("div");
    actions.className = "card-actions";
    if (rec.is_in_library) {
      const chip = document.createElement("span");
      chip.className = "lib-chip";
      chip.textContent = "In library";
      actions.appendChild(chip);
    } else {
      actions.appendChild(seerrButton(rec));
    }
    body.appendChild(actions);

    el.append(posterWrap, body);
    return el;
  }

  function seerrButton(rec) {
    const btn = document.createElement("button");
    btn.className = "seerr-btn";
    const key = `${rec.media_type}:${rec.tmdb_id}`;
    const known = rec.seerr_status || state.seerrStatusCache.get(key);
    if (known) {
      applySeerrState(btn, known);
      return btn;
    }
    btn.textContent = "+ Add to Seerr";
    if (!state.seerrReachable) {
      btn.disabled = true;
      btn.title = "Seerr is unreachable right now";
      return btn;
    }
    btn.addEventListener("click", () => addToSeerr(rec, btn));
    return btn;
  }

  function applySeerrState(btn, status) {
    const labels = { requested: "Requested ✓", pending: "Pending", available: "Available" };
    btn.textContent = labels[status] || status;
    btn.classList.add("is-state");
    btn.disabled = true;
  }

  async function addToSeerr(rec, btn) {
    if (rec.media_type === "movie") {
      return sendRequest(rec, btn, null);
    }
    if (state.tvRequestMode === "all") {
      const seasons = await fetchSeasons(rec.tmdb_id);
      return sendRequest(rec, btn, seasons.map((s) => s.seasonNumber));
    }
    if (state.tvRequestMode === "first") {
      return sendRequest(rec, btn, [1]);
    }
    openSeasonPicker(rec, btn); // ask (default)
  }

  async function sendRequest(rec, btn, seasons) {
    btn.disabled = true;
    btn.textContent = "Requesting…";
    try {
      const body = { type: rec.media_type, tmdb: rec.tmdb_id };
      if (seasons) body.seasons = seasons;
      const res = await api("/api/seerr/request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      state.seerrStatusCache.set(`${rec.media_type}:${rec.tmdb_id}`, res.status);
      applySeerrState(btn, res.status || "requested");
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "+ Add to Seerr";
      setStatus(err.message, true);
    }
  }

  async function fetchSeasons(tmdbId) {
    const data = await api(`/api/seerr/seasons?tmdb=${tmdbId}`);
    return data.seasons || [];
  }

  /* ---- season picker modal ------------------------------------------------ */
  let pickerContext = null;

  async function openSeasonPicker(rec, btn) {
    const seasons = await fetchSeasons(rec.tmdb_id);
    if (!seasons.length) {
      setStatus("Couldn't load seasons from Seerr — try again in a moment.", true);
      return;
    }
    pickerContext = { rec, btn };
    $("#season-title").textContent = `Choose seasons — ${rec.title}`;
    const list = $("#season-list");
    list.innerHTML = "";
    seasons.forEach((s) => {
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = s.seasonNumber;
      const name = document.createElement("span");
      name.textContent = s.name || `Season ${s.seasonNumber}`;
      const count = document.createElement("span");
      count.className = "ep-count";
      count.textContent = s.episodeCount ? `${s.episodeCount} ep` : "";
      label.append(box, name, count);
      list.appendChild(label);
    });
    $("#season-all").checked = false;
    show("#season-overlay");
  }

  $("#season-all").addEventListener("change", (e) => {
    document.querySelectorAll("#season-list input").forEach((box) => {
      box.checked = e.target.checked;
    });
  });
  $("#season-cancel").addEventListener("click", () => hide("#season-overlay"));
  $("#season-confirm").addEventListener("click", () => {
    const chosen = [...document.querySelectorAll("#season-list input:checked")]
      .map((box) => Number(box.value));
    if (!chosen.length) { setStatus("Pick at least one season.", true); return; }
    hide("#season-overlay");
    setStatus("");
    sendRequest(pickerContext.rec, pickerContext.btn, chosen);
  });

  /* ---- lazy Seerr status for visible cards ---------------------------------- */
  async function lazySeerrStatus(items) {
    if (!state.seerrReachable) return;
    for (const rec of items) {
      if (rec.is_in_library || rec.seerr_status || !rec.tmdb_id) continue;
      const key = `${rec.media_type}:${rec.tmdb_id}`;
      if (state.seerrStatusCache.has(key)) continue;
      try {
        const data = await api(`/api/seerr/status?type=${rec.media_type}&tmdb=${rec.tmdb_id}`);
        state.seerrStatusCache.set(key, data.status);
        if (data.status) {
          // refresh just this card's button if still on screen
          document.querySelectorAll(".card").forEach((el) => {
            const t = el.querySelector(".card-title");
            if (t && t.textContent.startsWith(rec.title)) {
              const btn = el.querySelector(".seerr-btn");
              if (btn && !btn.classList.contains("is-state")) applySeerrState(btn, data.status);
            }
          });
        }
      } catch (_) { return; } // Seerr went away — stop hammering it
    }
  }

  /* ---- filter / sort / pager -------------------------------------------------- */
  document.querySelectorAll(".seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.filter = btn.dataset.filter;
      state.page = 1;
      syncFilterButtons();
      loadRecommendations().catch((e) => setStatus(e.message, true));
    });
  });

  function syncFilterButtons() {
    document.querySelectorAll(".seg-btn").forEach((b) => {
      const f = b.dataset.filter;
      b.classList.toggle("is-active",
        f === state.filter || (f === "film" && state.filter === "movie"));
    });
  }

  $("#sort-select").addEventListener("change", (e) => {
    state.sort = e.target.value;
    state.page = 1;
    loadRecommendations().catch((err) => setStatus(err.message, true));
  });

  function renderPager() {
    pager.innerHTML = "";
    if (state.pages <= 1) return;
    const add = (label, page, opts = {}) => {
      const b = document.createElement("button");
      b.className = "page-btn" + (opts.current ? " is-current" : "");
      b.textContent = label;
      b.disabled = !!opts.disabled;
      if (!opts.disabled && !opts.current) {
        b.addEventListener("click", () => {
          state.page = page;
          loadRecommendations().catch((e) => setStatus(e.message, true));
          window.scrollTo({ top: 0, behavior: "smooth" });
        });
      }
      pager.appendChild(b);
    };
    add("‹", state.page - 1, { disabled: state.page <= 1 });
    for (let p = 1; p <= state.pages; p++) add(String(p), p, { current: p === state.page });
    add("›", state.page + 1, { disabled: state.page >= state.pages });
  }

  /* ---- settings + help panels --------------------------------------------------- */
  const show = (sel) => $(sel).classList.remove("hidden");
  const hide = (sel) => $(sel).classList.add("hidden");

  $("#btn-settings").addEventListener("click", async () => {
    try {
      const [settings, status] = await Promise.all([
        api("/api/settings"), api("/api/status"),
      ]);
      fillSettingsPanel(settings, status);
    } catch (_) { /* panel still opens with last values */ }
    show("#settings-overlay");
  });
  $("#btn-help").addEventListener("click", () => show("#help-overlay"));

  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => hide("#" + btn.dataset.close));
  });
  document.querySelectorAll(".overlay").forEach((ov) => {
    ov.addEventListener("click", (e) => { if (e.target === ov) ov.classList.add("hidden"); });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll(".overlay").forEach((ov) => ov.classList.add("hidden"));
  });

  function fillSettingsPanel(settings, status) {
    $("#set-recs").value = settings.recs_per_scan || 60;
    $("#set-tvmode").value = settings.tv_request_mode || "ask";
    $("#set-sort").value = settings.default_sort || "match";
    $("#set-filter").value = settings.default_filter || "all";

    const wrap = $("#settings-user-chips");
    wrap.innerHTML = "";
    const defaults = new Set((settings.default_user_ids || "").split(",").filter(Boolean));
    state.users.forEach((u) => {
      const label = document.createElement("label");
      label.className = "chip" + (defaults.has(u.id) ? " is-on" : "");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = defaults.has(u.id);
      box.dataset.uid = u.id;
      box.addEventListener("change", () => label.classList.toggle("is-on", box.checked));
      label.append(box, document.createTextNode(u.name));
      wrap.appendChild(label);
    });

    const dl = $("#status-dl");
    dl.innerHTML = "";
    const rows = [
      ["Provider", status.provider],
      ["Model", status.model],
      ["History source", status.history_source],
      ["OMDb today", `${status.omdb_today} / ${status.omdb_limit}`],
      ["Seerr", status.seerr_reachable ? "reachable" : "unreachable"],
    ];
    rows.forEach(([k, v]) => {
      const dt = document.createElement("dt"); dt.textContent = k;
      const dd = document.createElement("dd"); dd.textContent = v ?? "—";
      dl.append(dt, dd);
    });
  }

  $("#settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const ids = [...document.querySelectorAll("#settings-user-chips input:checked")]
      .map((box) => box.dataset.uid).join(",");
    try {
      const saved = await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          default_user_ids: ids,
          recs_per_scan: $("#set-recs").value,
          tv_request_mode: $("#set-tvmode").value,
          default_sort: $("#set-sort").value,
          default_filter: $("#set-filter").value,
        }),
      });
      state.tvRequestMode = saved.tv_request_mode;
      $("#settings-saved").textContent = "Saved.";
      setTimeout(() => { $("#settings-saved").textContent = ""; }, 2500);
    } catch (err) {
      $("#settings-saved").textContent = err.message;
    }
  });

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  init();
})();
