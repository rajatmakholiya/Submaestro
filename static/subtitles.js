"use strict";
const $ = (id) => document.getElementById(id);

let session = null;
let videoMeta = null;
let cues = [];
let activeCueIdx = -1;

// ---------------------------------------------------------------- helpers
function fmtT(sec) {
  if (sec == null || isNaN(sec)) sec = 0;
  const m = Math.floor(sec / 60);
  const s = (sec % 60);
  return `${m}:${s.toFixed(2).padStart(5, "0")}`;
}
function parseT(str) {
  if (str == null) return null;
  str = String(str).trim();
  if (/^\d+(\.\d+)?$/.test(str)) return parseFloat(str);
  const p = str.split(":").map(parseFloat);
  if (p.some(isNaN)) return null;
  return p.reduce((a, b) => a * 60 + b, 0);
}
function fmtBytes(b) {
  if (!b) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(1)} ${u[i]}`;
}
function escapeHtml(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
async function api(path, opts) {
  const r = await fetch(path, opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || d.error || `HTTP ${r.status}`);
  return d;
}
const post = (p, b) => api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });

// ---------------------------------------------------------------- gateway status
(async () => {
  try {
    const s = await api("/api/subtitles/status");
    const pill = $("gatewayPill");
    if (s.sync_engine === "deepgram") { pill.textContent = "✓ Deepgram word-sync"; pill.style.color = "var(--ok)"; }
    else if (s.gateway_configured) { pill.textContent = "✓ AI gateway ready"; pill.style.color = "var(--ok)"; }
    else { pill.textContent = "⚠ transcription key missing"; pill.style.color = "#ff8589"; }
  } catch {}
})();

// ---------------------------------------------------------------- upload
const dz = $("dropzone");
dz.addEventListener("click", () => $("fileInput").click());
$("fileInput").addEventListener("change", (e) => { if (e.target.files[0]) uploadFile(e.target.files[0]); });
["dragover", "dragenter"].forEach(ev => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach(ev => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) uploadFile(f); });

function uploadFile(file) {
  $("uploadError").classList.add("hidden");
  $("dropzone").classList.add("hidden");
  $("uploadProgress").classList.remove("hidden");
  const bar = $("uploadProgress").querySelector(".progress > div");
  const fd = new FormData();
  fd.append("file", file);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/subtitles/upload");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round(e.loaded / e.total * 100);
      bar.style.width = pct + "%";
      $("uploadLabel").textContent = `Uploading ${file.name} — ${pct}%`;
    }
  };
  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      videoMeta = JSON.parse(xhr.responseText);
      session = videoMeta.session;
      onUploaded();
    } else {
      let msg = "Upload failed";
      try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
      showUploadError(msg);
    }
  };
  xhr.onerror = () => showUploadError("Upload failed (network error)");
  xhr.send(fd);
}
function showUploadError(msg) {
  $("uploadProgress").classList.add("hidden");
  $("dropzone").classList.remove("hidden");
  $("uploadError").textContent = msg;
  $("uploadError").classList.remove("hidden");
}

function onUploaded() {
  $("uploadCard").classList.add("hidden");
  $("editorCard").classList.remove("hidden");
  const p = $("player");
  p.src = `/api/subtitles/video/${session}`;
  p.addEventListener("timeupdate", syncOverlay);
  p.addEventListener("loadedmetadata", syncOverlay);
  applyExportFrame();
  if (!videoMeta.has_audio) {
    $("transcribeError").textContent = "This video has no audio track, so there's nothing to transcribe. You can still add captions manually below.";
    $("transcribeError").classList.remove("hidden");
    $("cuesCard").classList.remove("hidden");
    $("renderBtn").disabled = false;
  }
}

// ---------------------------------------------------------------- transcribe
$("transcribeBtn").addEventListener("click", async () => {
  $("transcribeBtn").disabled = true;
  $("transcribeError").classList.add("hidden");
  $("transcribeStatus").classList.remove("hidden");
  const bar = $("transcribeStatus").querySelector(".progress > div");
  try {
    const { job_id } = await post("/api/subtitles/transcribe", { session });
    await pollJob(job_id, (j) => {
      $("transStatusLabel").textContent = j.detail || j.status;
      bar.style.width = (j.progress || 0) + "%";
    }, (j) => {
      cues = j.cues || [];
      $("transcribeStatus").classList.add("hidden");
      $("transcribeBtn").textContent = "↻ Regenerate subtitles";
      $("transcribeBtn").disabled = false;
      $("cuesCard").classList.remove("hidden");
      $("renderBtn").disabled = false;
      renderCues();
      syncOverlay();
    });
  } catch (e) {
    $("transcribeStatus").classList.add("hidden");
    $("transcribeError").textContent = e.message;
    $("transcribeError").classList.remove("hidden");
    $("transcribeBtn").disabled = false;
  }
});

async function pollJob(jobId, onProgress, onDone) {
  return new Promise((resolve, reject) => {
    const t = setInterval(async () => {
      let j;
      try { j = await api(`/api/subtitles/job/${jobId}`); } catch { return; }
      onProgress(j);
      if (j.status === "done") { clearInterval(t); onDone(j); resolve(j); }
      else if (j.status === "error") { clearInterval(t); reject(new Error(j.error || "failed")); }
    }, 700);
  });
}

// ---------------------------------------------------------------- cues editor
function invalidateOverlay() {
  // The overlay memoizes on (cue index, active word, style) — content edits
  // keep the same key, so force a rebuild whenever cues are mutated.
  $("capOverlay").dataset.key = "";
  syncOverlay();
}

function syncWordsToText(c) {
  // Text edits must flow into the word-timing list too — burned word-driven
  // styles (karaoke/active/…) render from `words`, not `text`, so stale words
  // would silently resurrect the pre-edit transcription in the output video.
  if (!c.words || !c.words.length) return;
  const tokens = (c.text || "").split(/\s+/).filter(Boolean);
  if (!tokens.length) { c.words = null; return; }
  if (tokens.length === c.words.length) {
    // Same word count (typo fixes) — keep each word's real timing.
    c.words.forEach((w, i) => { w.w = tokens[i]; });
  } else {
    // Word count changed — redistribute the cue's span evenly.
    const dur = Math.max(0.01, c.end - c.start);
    c.words = tokens.map((t, i) => ({
      w: t,
      s: +(c.start + dur * i / tokens.length).toFixed(3),
      e: +(c.start + dur * (i + 1) / tokens.length).toFixed(3),
    }));
  }
}

function mergeCue(i) {
  const a = cues[i], b = cues[i + 1];
  if (!b) return;
  a.text = (a.text.trim() + " " + b.text.trim()).trim();
  a.end = b.end;
  // Word timings survive a merge only when both halves still carry them.
  a.words = (a.words && a.words.length && b.words && b.words.length)
    ? a.words.concat(b.words) : null;
  cues.splice(i + 1, 1);
  renderCues(); invalidateOverlay();
}

function splitCue(i, charPos) {
  const c = cues[i];
  const text = c.text;
  const tokens = text.split(/\s+/).filter(Boolean);
  if (tokens.length < 2) return; // nothing to split
  // Word count before the cursor; cursor at the edges → split at the midpoint.
  let nBefore = text.slice(0, charPos).trim() ? text.slice(0, charPos).trim().split(/\s+/).length : 0;
  if (nBefore <= 0 || nBefore >= tokens.length) nBefore = Math.ceil(tokens.length / 2);
  const before = tokens.slice(0, nBefore).join(" ");
  const after = tokens.slice(nBefore).join(" ");

  let boundary, end1, w1 = null, w2 = null;
  if (c.words && c.words.length === tokens.length) {
    // Word timings intact: split exactly at the word boundary.
    w1 = c.words.slice(0, nBefore);
    w2 = c.words.slice(nBefore);
    boundary = w2[0].s;
    end1 = w1[w1.length - 1].e;
  } else {
    // Text was hand-edited (timings stale) — split time proportionally.
    const frac = nBefore / tokens.length;
    boundary = c.start + (c.end - c.start) * frac;
    end1 = boundary;
  }
  const second = { start: +(+boundary).toFixed(3), end: c.end, text: after, words: w2 };
  c.text = before;
  c.end = +(+end1).toFixed(3);
  c.words = w1;
  cues.splice(i + 1, 0, second);
  renderCues(); invalidateOverlay();
}

function renderCues() {
  $("cueCount").textContent = `${cues.length} lines`;
  const list = $("cuesList");
  list.innerHTML = "";
  cues.forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "cue";
    row.dataset.idx = i;
    row.innerHTML = `
      <input class="t start" value="${fmtT(c.start)}" title="start">
      <input class="t end" value="${fmtT(c.end)}" title="end">
      <input type="text" class="txt" value="${escapeHtml(c.text)}">
      <span class="cue-actions">
        <button class="split ghost" title="Split at cursor position in the text (middle if no cursor)">✂</button>
        <button class="merge ghost" title="Merge with the next line">⤵</button>
        <button class="del ghost" title="Delete line">✕</button>
      </span>`;
    const txt = row.querySelector(".txt");
    txt.addEventListener("input", (e) => {
      c.text = e.target.value;
      syncWordsToText(c);
      invalidateOverlay();
    });
    row.querySelector(".start").addEventListener("change", (e) => { const v = parseT(e.target.value); if (v != null) { c.start = v; e.target.value = fmtT(v); invalidateOverlay(); } });
    row.querySelector(".end").addEventListener("change", (e) => { const v = parseT(e.target.value); if (v != null) { c.end = v; e.target.value = fmtT(v); invalidateOverlay(); } });
    row.querySelectorAll(".t").forEach(el => el.addEventListener("dblclick", () => { $("player").currentTime = parseT(el.value) || 0; }));
    row.addEventListener("click", (e) => { if (!e.target.matches("input,button")) $("player").currentTime = c.start + 0.01; });
    row.querySelector(".split").addEventListener("click", (e) => { e.stopPropagation(); splitCue(i, txt.selectionStart ?? 0); });
    row.querySelector(".merge").addEventListener("click", (e) => { e.stopPropagation(); mergeCue(i); });
    row.querySelector(".del").addEventListener("click", (e) => { e.stopPropagation(); cues.splice(i, 1); renderCues(); invalidateOverlay(); });
    // Ctrl+Enter in the text = split at cursor (fast keyboard workflow)
    txt.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); splitCue(i, txt.selectionStart ?? 0); }
    });
    list.appendChild(row);
  });
}
$("addCue").addEventListener("click", () => {
  const t = $("player").currentTime || 0;
  cues.push({ start: +t.toFixed(2), end: +(t + 2).toFixed(2), text: "New caption", words: null });
  cues.sort((a, b) => a.start - b.start);
  renderCues(); syncOverlay();
});

// ---------------------------------------------------------------- style + overlay
const styleIds = ["stFont","stSize","stWeight","stPrimary","stAccent","stOutlineColor","stOutline","stShadow",
  "stBackground","stBoxOpacity","stWidth","stVpos","stItalic","stUppercase","stHighlight","stPopIn"];
styleIds.forEach(id => $(id).addEventListener("input", () => { updateStyleLabels(); syncOverlay(); }));

const WEIGHT_NAMES = { 100:"Thin", 200:"Extra Light", 300:"Light", 400:"Regular",
  500:"Medium", 600:"Semibold", 700:"Bold", 800:"Extra Bold", 900:"Black" };

function updateStyleLabels() {
  $("stSizeVal").textContent = $("stSize").value + "px";
  $("stWeightVal").textContent = WEIGHT_NAMES[$("stWeight").value] || $("stWeight").value;
  $("stOutlineVal").textContent = $("stOutline").value;
  $("stShadowVal").textContent = $("stShadow").value;
  $("stBoxOpacityVal").textContent = Math.round($("stBoxOpacity").value * 100) + "%";
  $("stWidthVal").textContent = $("stWidth").value + "%";
  $("stVposVal").textContent = Math.round($("stVpos").value) + "% from top";
}
updateStyleLabels();

function currentStyle() {
  return {
    font: $("stFont").value,
    size: +$("stSize").value,
    weight: +$("stWeight").value,
    primary: $("stPrimary").value,
    accent: $("stAccent").value,
    outline_color: $("stOutlineColor").value,
    outline: +$("stOutline").value,
    shadow: +$("stShadow").value,
    italic: $("stItalic").checked,
    uppercase: $("stUppercase").checked,
    background: $("stBackground").value,
    box_opacity: +$("stBoxOpacity").value,
    max_width: +$("stWidth").value,
    vpos: +$("stVpos").value,
    highlight: $("stHighlight").value,
    pop_in: $("stPopIn").checked,
  };
}

const PRESETS = {
  reels:   { font: "Arial Black", size: 66, weight: 900, primary: "#ffffff", accent: "#00e5ff", outline_color: "#000000", outline: 6, shadow: 2, uppercase: true, background: "none", highlight: "active", pop_in: true, max_width: 88, vpos: 50, export_ratio: "9:16", export_fit: "crop" },
  clean:   { font: "Arial", size: 54, weight: 700, primary: "#ffffff", accent: "#ffe24d", outline_color: "#000000", outline: 3, shadow: 1, uppercase: false, background: "none", highlight: "none", pop_in: false, max_width: 84, vpos: 90 },
  viral:   { font: "Impact", size: 78, weight: 900, primary: "#ffffff", accent: "#00e5ff", outline_color: "#000000", outline: 5, shadow: 2, uppercase: true, background: "none", highlight: "karaoke", pop_in: true, max_width: 80, vpos: 62 },
  boxed:   { font: "Segoe UI", size: 48, weight: 700, primary: "#ffffff", accent: "#ffe24d", outline_color: "#000000", outline: 0, shadow: 0, uppercase: false, background: "box", highlight: "none", pop_in: false, max_width: 84, vpos: 90 },
  minimal: { font: "Georgia", size: 44, weight: 400, primary: "#ffffff", accent: "#ffd166", outline_color: "#000000", outline: 2, shadow: 1, uppercase: false, background: "blur", highlight: "none", pop_in: false, max_width: 78, vpos: 90 },
};
// Apply a flat style/export object to every control it carries. Shared by the
// built-in presets and user-saved presets.
function applyStyleValues(p) {
  const set = (id, v) => { if (v !== undefined && v !== null) $(id).value = v; };
  const setChk = (id, v) => { if (v !== undefined && v !== null) $(id).checked = !!v; };
  set("stFont", p.font); set("stSize", p.size); set("stWeight", p.weight);
  set("stPrimary", p.primary); set("stAccent", p.accent);
  set("stOutlineColor", p.outline_color); set("stOutline", p.outline);
  set("stShadow", p.shadow); set("stBackground", p.background);
  set("stBoxOpacity", p.box_opacity); set("stWidth", p.max_width); set("stVpos", p.vpos);
  set("stHighlight", p.highlight);
  setChk("stItalic", p.italic); setChk("stUppercase", p.uppercase); setChk("stPopIn", p.pop_in);
  set("exportRatio", p.export_ratio); set("exportFit", p.export_fit); set("padColor", p.pad_color);
  updateStyleLabels();
  if (p.export_ratio || p.export_fit) applyExportFrame();
  $("capOverlay").dataset.key = "";
  syncOverlay();
}

document.querySelectorAll(".preset[data-preset]").forEach(b => b.addEventListener("click", () => {
  const p = PRESETS[b.dataset.preset]; if (!p) return;
  applyStyleValues(p);
}));

// ---------------------------------------------------------------- my presets
let customPresets = {};
async function refreshCustomPresets() {
  try { customPresets = (await api("/api/subtitles/presets")).presets || {}; }
  catch { customPresets = {}; }
  const holder = $("customPresetList");
  holder.innerHTML = "";
  for (const name of Object.keys(customPresets)) {
    const b = document.createElement("button");
    b.className = "preset custom-preset";
    b.title = "Apply this preset (× deletes it)";
    b.innerHTML = `${escapeHtml(name)}<span class="preset-del" title="Delete preset">×</span>`;
    b.addEventListener("click", async (e) => {
      if (e.target.classList.contains("preset-del")) {
        e.stopPropagation();
        if (!confirm(`Delete preset "${name}"?`)) return;
        await api(`/api/subtitles/presets/${encodeURIComponent(name)}`, { method: "DELETE" });
        refreshCustomPresets();
        return;
      }
      const p = customPresets[name] || {};
      applyStyleValues({ ...(p.style || {}), ...(p.export || {}) });
    });
    holder.appendChild(b);
  }
}
$("savePresetBtn").addEventListener("click", async () => {
  const name = (window.prompt("Preset name:") || "").trim();
  if (!name) return;
  if (customPresets[name] && !confirm(`"${name}" exists — overwrite it?`)) return;
  await post("/api/subtitles/presets", { name, style: currentStyle(), export: currentExport() });
  refreshCustomPresets();
});
refreshCustomPresets();

// vertical quick-set buttons (Top / Center / Bottom)
document.querySelectorAll(".vq").forEach(b => b.addEventListener("click", () => {
  $("stVpos").value = b.dataset.v; updateStyleLabels(); syncOverlay();
}));

// ---------------------------------------------------------------- export frame
function currentExport() {
  return {
    export_ratio: $("exportRatio").value,
    export_fit: $("exportFit").value,
    pad_color: $("padColor").value,
  };
}
function ratioWH(ratio) {
  if (!ratio || ratio === "source") {
    const w = videoMeta?.width || 16, h = videoMeta?.height || 9;
    return [w, h];
  }
  const [a, b] = ratio.split(":").map(Number);
  return [a, b];
}
function applyExportFrame() {
  const wrap = $("videoWrap");
  const ex = currentExport();
  const [rw, rh] = ratioWH(ex.export_ratio);
  wrap.style.aspectRatio = `${rw} / ${rh}`;
  wrap.style.height = "clamp(240px, 56vh, 480px)";
  wrap.style.width = "auto";
  wrap.style.maxWidth = "100%";
  wrap.style.margin = "0 auto";
  const p = $("player");
  p.style.width = "100%"; p.style.height = "100%"; p.style.maxHeight = "none";
  p.style.objectFit = ex.export_fit === "pad" ? "contain" : "cover";
  wrap.style.background = ex.export_fit === "pad" ? ex.pad_color : "#000";
  $("padColorRow").classList.toggle("hidden", ex.export_fit !== "pad");
  syncOverlay();
}
["exportRatio","exportFit","padColor"].forEach(id =>
  $(id).addEventListener("input", applyExportFrame));

// ---------------------------------------------------------------- sync offset
function updateSyncLabel() {
  const v = syncOffset();
  $("syncOffsetVal").textContent = (v > 0 ? "+" : "") + v.toFixed(2) + "s"
    + (v === 0 ? " (in sync)" : v > 0 ? " (later)" : " (earlier)");
}
$("syncOffset").addEventListener("input", () => { updateSyncLabel(); syncOverlay(); });
document.querySelectorAll(".sync-nudge").forEach(b => b.addEventListener("click", () => {
  const el = $("syncOffset");
  el.value = Math.max(-3, Math.min(3, +el.value + +b.dataset.d)).toFixed(2);
  updateSyncLabel(); syncOverlay();
}));
$("syncReset").addEventListener("click", () => {
  $("syncOffset").value = 0; updateSyncLabel(); syncOverlay();
});
updateSyncLabel();

function syncOffset() { return +($("syncOffset")?.value || 0); }

function activeCue(t) {
  for (let i = 0; i < cues.length; i++) if (t >= cues[i].start && t <= cues[i].end) return i;
  return -1;
}

function syncOverlay() {
  const p = $("player");
  const overlay = $("capOverlay");
  // Shift the comparison time by the sync offset so captions lead/lag the
  // audio uniformly (same shift applied at render). +offset = show later.
  const t = (p.currentTime || 0) - syncOffset();
  const idx = activeCue(t);

  if (idx !== activeCueIdx) {
    document.querySelectorAll(".cue").forEach(el => el.classList.toggle("active", +el.dataset.idx === idx));
    const act = document.querySelector(`.cue[data-idx="${idx}"]`);
    if (act) act.scrollIntoView({ block: "nearest", behavior: "smooth" });
    activeCueIdx = idx;
  }
  if (idx < 0) { overlay.innerHTML = ""; overlay.dataset.key = ""; return; }

  const st = currentStyle();
  // Scale relative to the output frame (the wrap), which equals the export frame.
  const vh = ($("videoWrap").clientHeight) || p.clientHeight || 300;
  const scale = vh / 1080;
  const fontPx = Math.max(10, st.size * scale);
  const outPx = st.outline * scale;

  // Precise vertical: centre the caption block on vpos% of the frame height,
  // matching the ASS \an5\pos anchor. Width: symmetric side padding.
  const sidePad = (100 - st.max_width) / 2;
  overlay.style.bottom = "auto";
  overlay.style.top = st.vpos + "%";
  overlay.style.transform = "translateY(-50%)";
  overlay.style.padding = `0 ${sidePad}%`;

  const c = cues[idx];
  const ws = (c.words && c.words.length) ? c.words : null;
  const mode = ws ? st.highlight : "none";
  const T = (w) => escapeHtml(st.uppercase ? w.w.toUpperCase() : w.w);

  // Active word index within this cue (-1 before the first word is spoken).
  let awi = -1;
  if (ws) for (let i = 0; i < ws.length; i++) if (t >= ws[i].s) awi = i;

  // Only rebuild the DOM when the visible state changes — this is what lets
  // the CSS pop/appear animations fire once per word/cue instead of restarting
  // every frame of the rAF loop.
  const key = [idx, mode === "none" || mode === "karaoke" ? (mode === "karaoke" ? awi : "") : awi,
               JSON.stringify(st)].join("|");
  if (key === overlay.dataset.key) return;
  overlay.dataset.key = key;

  // Per-mode word rendering (mirrors the burned ASS output).
  let inner;
  if (mode === "karaoke") {
    inner = ws.map((w, i) =>
      `<span style="color:${i <= awi ? st.accent : st.primary}">${T(w)}</span>`).join(" ");
  } else if (mode === "active") {
    inner = ws.map((w, i) => i === awi
      ? `<span class="w-active" style="color:${st.accent};font-weight:900">${T(w)}</span>`
      : `<span>${T(w)}</span>`).join(" ");
  } else if (mode === "wordbyword") {
    inner = ws.map((w, i) =>
      `<span class="${i === awi ? "w-appear" : ""}" style="visibility:${i <= awi ? "visible" : "hidden"}">${T(w)}</span>`).join(" ");
  } else if (mode === "focus") {
    const w = ws[Math.max(0, awi)];
    inner = `<span class="w-focus" style="display:inline-block">${T(w)}</span>`;
  } else {
    inner = escapeHtml(st.uppercase ? (c.text || "").toUpperCase() : (c.text || ""));
  }

  const focusScale = mode === "focus" ? 1.3 : 1;
  const shadow = st.shadow > 0 ? `${1.5 * scale}px ${1.5 * scale}px ${st.shadow * scale}px rgba(0,0,0,.9)` : "none";
  let bg = "transparent", pad = "0", radius = "0", bd = "";
  if (st.background === "box") { bg = "rgba(0,0,0,0.82)"; pad = `${4*scale}px ${14*scale}px`; radius = `${8*scale}px`; }
  else if (st.background === "blur") { bg = "rgba(0,0,0,0.4)"; pad = `${4*scale}px ${14*scale}px`; radius = `${8*scale}px`; bd = "backdrop-filter:blur(6px);"; }

  // Pop-in punch fires when the CUE changes (not on word updates within it).
  const cueChanged = String(idx) !== overlay.dataset.cue;
  overlay.dataset.cue = String(idx);
  const popClass = st.pop_in && cueChanged ? " cap-pop" : "";

  overlay.innerHTML = `<span class="cap-text${popClass}" style="
    font-family:'${st.font}',sans-serif;
    font-size:${fontPx * focusScale}px;
    font-weight:${st.weight};
    font-style:${st.italic ? "italic" : "normal"};
    color:${st.primary};
    -webkit-text-stroke:${outPx}px ${st.outline_color};
    paint-order:stroke fill;
    text-shadow:${shadow};
    background:${bg};padding:${pad};border-radius:${radius};${bd}
  ">${inner}</span>`;
}
window.addEventListener("resize", () => { $("capOverlay").dataset.key = ""; syncOverlay(); });

// Smooth playback tracking: timeupdate fires ~4×/s which is too coarse for
// word-level styles — run a rAF loop while the video plays.
(() => {
  const p = $("player");
  let rafId = null;
  const loop = () => { syncOverlay(); if (!p.paused && !p.ended) rafId = requestAnimationFrame(loop); };
  p.addEventListener("play", () => { cancelAnimationFrame(rafId); loop(); });
  p.addEventListener("pause", () => cancelAnimationFrame(rafId));
})();

// ---------------------------------------------------------------- render
$("renderBtn").addEventListener("click", async () => {
  if (!cues.length) { alert("No captions to render. Generate or add some first."); return; }
  $("renderBtn").disabled = true;
  $("renderStatus").classList.remove("hidden");
  $("renderResult").innerHTML = "";
  const bar = $("renderStatus").querySelector(".progress > div");
  bar.style.width = "30%";
  try {
    const { job_id } = await post("/api/subtitles/render", {
      session, cues, style: currentStyle(),
      mode: $("renderMode").value, crf: +$("renderCrf").value,
      sync_offset: syncOffset(),
      ...currentExport(),
    });
    await pollJob(job_id, (j) => {
      $("renderStatusLabel").textContent = j.detail || j.status;
      bar.style.width = (j.status === "rendering" ? 65 : (j.progress || 30)) + "%";
    }, (j) => {
      bar.style.width = "100%";
      $("renderStatusLabel").textContent = `Done · ${fmtBytes(j.size)}`;
      $("renderResult").innerHTML =
        `<div class="row" style="margin-top:10px;gap:14px">
          <a class="primary" style="padding:10px 16px;border-radius:9px;text-decoration:none" href="/files/${encodeURIComponent(j.file)}" download>💾 Download video</a>
          <a href="/files/${encodeURIComponent(j.srt)}" download>Download .srt</a>
        </div>`;
      $("renderBtn").disabled = false;
    });
  } catch (e) {
    $("renderStatusLabel").textContent = "";
    $("renderResult").innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
    $("renderBtn").disabled = false;
  }
});
