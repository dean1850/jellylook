/* jellylook frontend — vanilla JS, no framework, no localStorage. */
(() => {
  "use strict";

  const IMG = "https://image.tmdb.org/t/p/w92";
  const YOUTUBE = "https://www.youtube.com/watch?v=";
  // Cards animate in on a stagger, but only the first screenful: past that the
  // delay would outlast the user's patience rather than feel responsive.
  const STAGGER_MS = 26;
  const STAGGER_MAX = 12;

  const state = {
    users: [],
    selected: new Set(),
    filter: "all",          // all | tv | film | saved | hidden
    scan: "latest",         // latest | all | <scan_id>
    sort: "match",
    page: 1,
    pages: 0,
    scanning: false,
    tvRequestMode: "ask",
    seerrReachable: true,
    seerrStatusCache: new Map(),  // "type:tmdb" -> status|null
    genres: [],                   // facets for the current result set
    filters: emptyFilters(),
    counts: { saved: 0, hidden: 0 },
    allTimeTotal: 0,
    scans: [],
  };

  function emptyFilters() {
    return {
      genres: new Set(), year_min: "", runtime_max: "", imdb_min: "",
      in_library: "any", requested: "any", streaming: "any",
    };
  }

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

  const userParam = () => [...state.selected].join(",");
  const isFeedbackView = () => state.filter === "saved" || state.filter === "hidden";

  /* ---- users + settings bootstrap -------------------------------------- */
  async function init() {
    try {
      const [users, settings, status] = await Promise.all([
        api("/api/users"), api("/api/settings"), api("/api/status"),
      ]);
      state.users = users;
      state.tvRequestMode = settings.tv_request_mode || "ask";
      state.sort = settings.default_sort || "match";
      state.filter = settings.default_filter === "movie"
        ? "film" : (settings.default_filter || "all");
      state.seerrReachable = !!status.seerr_reachable;
      $("#sort-select").value = state.sort;
      syncFilterButtons();
      const defaults = (settings.default_user_ids || "").split(",").filter(Boolean);
      users.forEach((u) => { if (defaults.includes(u.id)) state.selected.add(u.id); });
      renderUserChips();
      renderLastScan(status.last_scans);
      fillSettingsPanel(settings, status);
      await refreshSidebarState();
      await loadRecommendations();
      if (!grid.children.length && !isFeedbackView()) {
        setStatus("Pick who's watching and hit Scan.");
      }
    } catch (err) {
      setStatus(`Can't reach the jellylook API. ${err.message}`, true);
    }
  }

  /** Feedback counts and the scan list both depend on who is selected. */
  async function refreshSidebarState() {
    try {
      const data = await api(`/api/scans?users=${encodeURIComponent(userParam())}`);
      state.scans = data.scans || [];
      state.allTimeTotal = data.all_time_total || 0;
      renderScanSelect();
    } catch (_) { /* the picker just keeps its last contents */ }
    try {
      const status = await api(`/api/status?users=${encodeURIComponent(userParam())}`);
      state.counts = status.feedback || state.counts;
      renderTabCounts();
    } catch (_) { /* counts are cosmetic */ }
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
      box.addEventListener("change", async () => {
        box.checked ? state.selected.add(u.id) : state.selected.delete(u.id);
        label.classList.toggle("is-on", box.checked);
        updateScanButton();
        // Feedback is per user, so changing the selection changes both the
        // saved/hidden lists and which titles are filtered out of every view.
        state.page = 1;
        await refreshSidebarState();
        loadRecommendations().catch((e) => setStatus(e.message, true));
      });
      label.append(box, document.createTextNode(u.name));
      wrap.appendChild(label);
    });
    updateScanButton();
  }

  function updateScanButton() {
    scanBtn.disabled = state.scanning || state.selected.size === 0;
  }

  function renderTabCounts() {
    const label = (base, n) => (n ? `${base} ${n}` : base);
    $("#tab-saved").textContent = label("♡ Saved", state.counts.saved);
    $("#tab-hidden").textContent = label("Hidden", state.counts.hidden);
  }

  function renderScanSelect() {
    const sel = $("#scan-select");
    sel.innerHTML = "";
    const add = (value, text) => {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = text;
      sel.appendChild(opt);
    };
    add("latest", "Latest scan");
    add("all", `All time${state.allTimeTotal ? ` · ${state.allTimeTotal}` : ""}`);
    state.scans.slice(0, 20).forEach((scan, i) => {
      if (i === 0) return;  // that is "Latest scan" under another name
      const names = (scan.source_users || "").split(",")
        .map((id) => (state.users.find((u) => u.id === id) || {}).name)
        .filter(Boolean).join(", ");
      add(scan.id, `${shortDate(scan.created_at)}${names ? ` · ${names}` : ""} · ${scan.item_count}`);
    });
    sel.value = state.scan;
    if (sel.value !== state.scan) { state.scan = "latest"; sel.value = "latest"; }
  }

  function shortDate(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "?"
      : d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
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
        state.scan = "latest";
        if (isFeedbackView()) state.filter = "all";
        syncFilterButtons();
        state.seerrStatusCache.clear();
        setStatus("");
        const status = await api(`/api/status?users=${encodeURIComponent(userParam())}`);
        state.seerrReachable = !!status.seerr_reachable;
        renderLastScan(status.last_scans);
        await refreshSidebarState();
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
  function buildQuery() {
    const p = new URLSearchParams({ page: state.page });
    if (state.selected.size) p.set("users", userParam());
    if (isFeedbackView()) {
      p.set("verdict", state.filter);
      return p;
    }
    p.set("type", state.filter);
    p.set("sort", state.sort);
    p.set("scan", state.scan);
    const f = state.filters;
    if (f.genres.size) p.set("genre", [...f.genres].join(","));
    if (f.year_min) p.set("year_min", f.year_min);
    if (f.runtime_max) p.set("runtime_max", f.runtime_max);
    if (f.imdb_min) p.set("imdb_min", f.imdb_min);
    if (f.in_library !== "any") p.set("in_library", f.in_library);
    if (f.requested !== "any") p.set("requested", f.requested);
    if (f.streaming !== "any") p.set("streaming", f.streaming);
    return p;
  }

  function activeFilterCount() {
    const f = state.filters;
    return (f.genres.size ? 1 : 0) + (f.year_min ? 1 : 0) + (f.runtime_max ? 1 : 0)
      + (f.imdb_min ? 1 : 0) + (f.in_library !== "any" ? 1 : 0)
      + (f.requested !== "any" ? 1 : 0) + (f.streaming !== "any" ? 1 : 0);
  }

  async function loadRecommendations() {
    const feedbackView = isFeedbackView();
    $("#btn-filters").hidden = feedbackView;
    $("#scan-select").closest(".seg").hidden = feedbackView;
    if (feedbackView) closeDrawer();

    if (feedbackView && !state.selected.size) {
      grid.innerHTML = "";
      pager.innerHTML = "";
      setStatus("Tick who's watching to see their saved and hidden titles.");
      return;
    }

    const data = await api(`/api/recommendations?${buildQuery()}`);
    state.pages = data.pages;
    if (data.facets) { state.genres = data.facets.genres || []; renderGenreChips(); }
    renderGrid(data.items);
    renderPager();
    updateFilterBadge();

    if (data.total === 0) {
      if (feedbackView) {
        setStatus(state.filter === "saved"
          ? "Nothing saved yet — tap ♡ on a card to keep it here."
          : "Nothing hidden yet — tap ✕ on a card you don't want suggested again.");
      } else if (activeFilterCount()) {
        setStatus("No titles match these filters." + droppedNote(data));
      } else if (data.scan_id || state.scan === "all") {
        setStatus("No recommendations here yet.");
      }
    } else {
      setStatus(droppedNote(data).trim());
    }
    lazySeerrStatus(data.items);
  }

  /** Pre-upgrade rows have no genre data; say so rather than let them vanish. */
  function droppedNote(data) {
    if (!data.dropped_no_genre) return "";
    const n = data.dropped_no_genre;
    return ` ${n} older title${n === 1 ? "" : "s"} hidden — scanned before ` +
           `genre data was recorded.`;
  }

  function updateFilterBadge() {
    const n = activeFilterCount();
    const badge = $("#filter-count");
    badge.textContent = n;
    badge.hidden = n === 0;
    $("#btn-filters").classList.toggle("is-active", n > 0);
  }

  function renderGrid(items) {
    grid.innerHTML = "";
    const frag = document.createDocumentFragment();
    items.forEach((rec, i) => frag.appendChild(card(rec, i)));
    grid.appendChild(frag);
  }

  function card(rec, i) {
    const el = document.createElement("article");
    el.className = "card";
    el.style.setProperty("--i", Math.min(i, STAGGER_MAX));
    el.dataset.key = `${rec.media_type}:${rec.tmdb_id}`;

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
      chip.className = "lib-chip corner";
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
    if (rec.trailer_key) posterWrap.appendChild(trailerButton(rec));

    const body = document.createElement("div");
    body.className = "card-body";

    const title = document.createElement("h3");
    title.className = "card-title";
    title.textContent = rec.title;
    if (rec.year) {
      const year = document.createElement("span");
      year.className = "year";
      year.textContent = ` ’${String(rec.year).slice(-2)}`;
      title.appendChild(year);
    }
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
    if (rec.runtime) {
      const rt = document.createElement("span");
      rt.className = "tmdb-score";
      rt.textContent = formatRuntime(rec.runtime, rec.media_type);
      ratings.appendChild(rt);
    }
    body.appendChild(ratings);

    const providers = providerRow(rec.providers);
    if (providers) body.appendChild(providers);

    if (rec.because_of) {
      const because = document.createElement("p");
      because.className = "because";
      because.append("Because you watched ");
      const strong = document.createElement("strong");
      strong.textContent = rec.because_of;
      because.appendChild(strong);
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
    } else if (rec.tmdb_id) {
      actions.appendChild(seerrButton(rec));
    }
    actions.appendChild(feedbackRow(rec, el));
    body.appendChild(actions);

    el.append(posterWrap, body);
    return el;
  }

  function formatRuntime(mins, mediaType) {
    const text = mins >= 60 ? `${Math.floor(mins / 60)}h ${mins % 60}m`.replace(" 0m", "")
                            : `${mins}m`;
    return mediaType === "tv" ? `~${text}/ep` : text;
  }

  function trailerButton(rec) {
    const btn = document.createElement("button");
    btn.className = "trailer-btn";
    btn.type = "button";
    btn.setAttribute("aria-label", `Trailer for ${rec.title}`);
    btn.title = "Watch the trailer";
    btn.textContent = "▶";
    btn.addEventListener("click", () => {
      $("#trailer-title").textContent = `Trailer — ${rec.title}`;
      $("#trailer-link").href = YOUTUBE + rec.trailer_key;
      show("#trailer-overlay");
    });
    return btn;
  }

  /** Streaming availability. The data is JustWatch's, so it is credited here. */
  function providerRow(raw) {
    if (!raw) return null;
    let data;
    try { data = JSON.parse(raw); } catch (_) { return null; }
    const list = data.flatrate || data.free || data.ads;
    if (!list || !list.length) return null;

    const row = document.createElement("div");
    row.className = "provider-row";
    const label = document.createElement("span");
    label.className = "provider-label";
    label.textContent = data.flatrate ? "Streaming" : "Free";
    row.appendChild(label);

    list.slice(0, 4).forEach((p) => {
      if (p.logo) {
        const img = document.createElement("img");
        img.className = "provider-logo";
        img.src = IMG + p.logo;
        img.alt = p.name || "";
        img.title = p.name || "";
        img.loading = "lazy";
        row.appendChild(img);
      } else {
        const chip = document.createElement("span");
        chip.className = "provider-name";
        chip.textContent = p.name;
        row.appendChild(chip);
      }
    });
    const credit = document.createElement("span");
    credit.className = "justwatch";
    credit.textContent = "JustWatch";
    credit.title = "Streaming availability data provided by JustWatch";
    row.appendChild(credit);
    return row;
  }

  /* ---- feedback ------------------------------------------------------------ */
  function feedbackRow(rec, cardEl) {
    const row = document.createElement("div");
    row.className = "feedback-row";
    if (!rec.tmdb_id) return row;

    const save = mkFeedbackBtn("♡", "Save for later", rec.verdict === "saved");
    const hide = mkFeedbackBtn("✕", "Not for me — never suggest again",
                               rec.verdict === "hidden");

    if (!state.selected.size) {
      [save, hide].forEach((b) => {
        b.disabled = true;
        b.title = "Tick who's watching first — feedback is saved per viewer";
      });
      row.append(save, hide);
      return row;
    }

    save.addEventListener("click", () =>
      sendFeedback(rec, cardEl, rec.verdict === "saved" ? null : "saved", save));
    hide.addEventListener("click", () =>
      sendFeedback(rec, cardEl, rec.verdict === "hidden" ? null : "hidden", hide));
    row.append(save, hide);
    return row;
  }

  function mkFeedbackBtn(glyph, label, isOn) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "fb-btn" + (isOn ? " is-on" : "");
    btn.textContent = glyph;
    btn.title = label;
    btn.setAttribute("aria-label", label);
    btn.setAttribute("aria-pressed", String(!!isOn));
    return btn;
  }

  async function sendFeedback(rec, cardEl, verdict, btn) {
    btn.disabled = true;
    btn.classList.add("is-busy");
    try {
      const res = await api("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_ids: [...state.selected],
          media_type: rec.media_type,
          tmdb_id: rec.tmdb_id,
          verdict,
          title: rec.title,
          year: rec.year,
          poster_url: rec.poster_url,
        }),
      });
      state.counts = res.counts || state.counts;
      renderTabCounts();
      rec.verdict = res.verdict;

      // A card leaves the view when its verdict no longer belongs there:
      // hidden in a browsing view, or un-marked inside Saved/Hidden.
      const leaves = isFeedbackView()
        ? res.verdict !== state.filter
        : res.verdict === "hidden";
      if (leaves) return removeCard(cardEl);

      btn.classList.toggle("is-on", !!res.verdict);
      btn.setAttribute("aria-pressed", String(!!res.verdict));
      btn.classList.add("pop");
      setTimeout(() => btn.classList.remove("pop"), 260);
    } catch (err) {
      setStatus(err.message, true);
    } finally {
      btn.disabled = false;
      btn.classList.remove("is-busy");
    }
  }

  function removeCard(cardEl) {
    cardEl.classList.add("is-leaving");
    const done = () => {
      cardEl.remove();
      if (!grid.querySelector(".card")) {
        // Emptied the page — pull the next one in rather than leave a void.
        loadRecommendations().catch((e) => setStatus(e.message, true));
      }
    };
    // transitionend can be skipped entirely under prefers-reduced-motion, so
    // a timer guarantees the node is actually removed either way.
    cardEl.addEventListener("transitionend", done, { once: true });
    setTimeout(done, 260);
  }

  /* ---- seerr ------------------------------------------------------------- */
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
          // Cards carry their identity, so the right button is addressable
          // directly rather than matched on displayed text.
          const el = grid.querySelector(`.card[data-key="${CSS.escape(key)}"]`);
          const btn = el && el.querySelector(".seerr-btn");
          if (btn && !btn.classList.contains("is-state")) applySeerrState(btn, data.status);
        }
      } catch (_) { return; } // Seerr went away — stop hammering it
    }
  }

  /* ---- filter / sort / pager -------------------------------------------------- */
  document.querySelectorAll(".seg-btn[data-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.filter = btn.dataset.filter;
      state.page = 1;
      syncFilterButtons();
      setStatus("");
      loadRecommendations().catch((e) => setStatus(e.message, true));
    });
  });

  function syncFilterButtons() {
    document.querySelectorAll(".seg-btn[data-filter]").forEach((b) => {
      b.classList.toggle("is-active", b.dataset.filter === state.filter);
    });
  }

  $("#sort-select").addEventListener("change", (e) => {
    state.sort = e.target.value;
    state.page = 1;
    loadRecommendations().catch((err) => setStatus(err.message, true));
  });

  $("#scan-select").addEventListener("change", (e) => {
    state.scan = e.target.value;
    state.page = 1;
    loadRecommendations().catch((err) => setStatus(err.message, true));
  });

  /* ---- filter drawer ----------------------------------------------------------- */
  const drawer = $("#filter-drawer");

  function openDrawer() {
    drawer.hidden = false;
    // Height is measured, then animated to it, so the panel can grow with its
    // contents without a hard-coded max-height clipping the genre chips.
    requestAnimationFrame(() => {
      drawer.style.maxHeight = `${drawer.scrollHeight}px`;
      drawer.classList.add("is-open");
    });
    $("#btn-filters").setAttribute("aria-expanded", "true");
  }

  function closeDrawer() {
    drawer.style.maxHeight = "0px";
    drawer.classList.remove("is-open");
    $("#btn-filters").setAttribute("aria-expanded", "false");
    setTimeout(() => { if (!drawer.classList.contains("is-open")) drawer.hidden = true; }, 200);
  }

  $("#btn-filters").addEventListener("click", () => {
    drawer.classList.contains("is-open") ? closeDrawer() : openDrawer();
  });

  function renderGenreChips() {
    const wrap = $("#genre-chips");
    wrap.innerHTML = "";
    if (!state.genres.length) {
      wrap.innerHTML = '<span class="meta">No genre data in these results yet.</span>';
      return;
    }
    state.genres.forEach((g) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip genre-chip" + (state.filters.genres.has(g.id) ? " is-on" : "");
      btn.textContent = `${g.name} ${g.count}`;
      btn.addEventListener("click", () => {
        state.filters.genres.has(g.id)
          ? state.filters.genres.delete(g.id)
          : state.filters.genres.add(g.id);
        btn.classList.toggle("is-on");
        state.page = 1;
        applyFilters();
      });
      wrap.appendChild(btn);
    });
    if (drawer.classList.contains("is-open")) {
      drawer.style.maxHeight = `${drawer.scrollHeight}px`;
    }
  }

  function applyFilters() {
    loadRecommendations().catch((e) => setStatus(e.message, true));
  }

  const bindSelect = (sel, key) => $(sel).addEventListener("change", (e) => {
    state.filters[key] = e.target.value;
    state.page = 1;
    applyFilters();
  });
  bindSelect("#f-decade", "year_min");
  bindSelect("#f-runtime", "runtime_max");
  bindSelect("#f-imdb", "imdb_min");
  bindSelect("#f-library", "in_library");
  bindSelect("#f-streaming", "streaming");
  bindSelect("#f-requested", "requested");

  $("#btn-reset-filters").addEventListener("click", () => {
    state.filters = emptyFilters();
    ["#f-decade", "#f-runtime", "#f-imdb"].forEach((s) => { $(s).value = ""; });
    ["#f-library", "#f-streaming", "#f-requested"].forEach((s) => { $(s).value = "any"; });
    document.querySelectorAll(".genre-chip.is-on").forEach((c) => c.classList.remove("is-on"));
    state.page = 1;
    applyFilters();
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
    // Long histories can run to dozens of pages, so the run is windowed
    // instead of printing every number.
    const from = Math.max(1, state.page - 3);
    const to = Math.min(state.pages, from + 6);
    if (from > 1) add("1", 1, {});
    for (let p = from; p <= to; p++) add(String(p), p, { current: p === state.page });
    if (to < state.pages) add(String(state.pages), state.pages, {});
    add("›", state.page + 1, { disabled: state.page >= state.pages });
  }

  /* ---- settings + help panels --------------------------------------------------- */
  const show = (sel) => $(sel).classList.remove("hidden");
  const hide = (sel) => $(sel).classList.add("hidden");

  $("#btn-settings").addEventListener("click", async () => {
    try {
      const [settings, status] = await Promise.all([
        api("/api/settings"),
        api(`/api/status?users=${encodeURIComponent(userParam())}`),
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
    const fb = status.feedback || { saved: 0, hidden: 0 };
    const rows = [
      ["Provider", status.provider],
      ["Model", status.model],
      ["History source", status.history_source],
      ["Streaming region", status.region],
      ["OMDb today", `${status.omdb_today} / ${status.omdb_limit}`],
      ["Seerr", status.seerr_reachable ? "reachable" : "unreachable"],
      ["Saved / hidden", `${fb.saved} / ${fb.hidden}`],
      ["Kept for", `${status.retention_days} days · purged at ${status.max_retention_days}`],
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

  init();
})();
