"use strict";

const $ = (id) => document.getElementById(id);

let videoInfo = null;
let mode = "video";
const pollTimers = {};

// ---------------------------------------------------------------- helpers

function fmtTime(sec) {
  if (sec == null || isNaN(sec)) return "?";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

function parseTime(str) {
  if (!str || !str.trim()) return null;
  const parts = str.trim().split(":").map(Number);
  if (parts.some(isNaN)) return null;
  let sec = 0;
  for (const p of parts) sec = sec * 60 + p;
  return sec;
}

function fmtBytes(b) {
  if (!b) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(1)} ${units[i]}`;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
}

function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------- probe

$("probeBtn").addEventListener("click", probe);
$("urlInput").addEventListener("keydown", (e) => { if (e.key === "Enter") probe(); });

async function probe() {
  const url = $("urlInput").value.trim();
  if (!url) return;
  const btn = $("probeBtn");
  btn.disabled = true;
  btn.textContent = "Fetching…";
  $("probeError").classList.add("hidden");
  $("videoCard").classList.add("hidden");
  try {
    videoInfo = await post("/api/probe", { url });
    renderVideo();
  } catch (e) {
    $("probeError").textContent = e.message;
    $("probeError").classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "Fetch";
  }
}

function renderVideo() {
  $("thumb").src = videoInfo.thumbnail || "";
  $("videoTitle").textContent = videoInfo.title || "(untitled)";
  const bits = [];
  if (videoInfo.uploader) bits.push(videoInfo.uploader);
  if (videoInfo.duration) bits.push(fmtTime(videoInfo.duration));
  if (videoInfo.client_used && videoInfo.client_used !== "default")
    bits.push(`via ${videoInfo.client_used} client`);
  $("videoMeta").textContent = bits.join(" · ");

  const sel = $("videoQuality");
  sel.innerHTML = '<option value="best">Best available</option>';
  for (const h of videoInfo.heights || []) {
    const opt = document.createElement("option");
    opt.value = h;
    opt.textContent = `${h}p`;
    sel.appendChild(opt);
  }

  // reset trim
  $("trimEnable").checked = false;
  $("trimControls").classList.add("hidden");
  const dur = videoInfo.duration || 0;
  $("rangeStart").max = dur; $("rangeEnd").max = dur;
  $("rangeStart").value = 0; $("rangeEnd").value = dur;
  $("trimStart").value = fmtTime(0);
  $("trimEnd").value = fmtTime(dur);
  updateSliderFill();

  $("videoCard").classList.remove("hidden");
}

// ---------------------------------------------------------------- tabs

$("tabVideo").addEventListener("click", () => setMode("video"));
$("tabAudio").addEventListener("click", () => setMode("audio"));

function setMode(m) {
  mode = m;
  $("tabVideo").classList.toggle("active", m === "video");
  $("tabAudio").classList.toggle("active", m === "audio");
  $("videoOpts").classList.toggle("hidden", m !== "video");
  $("audioOpts").classList.toggle("hidden", m !== "audio");
  $("preciseCutLabel").classList.toggle("hidden", m === "audio");
  $("audioTrimNote").classList.toggle("hidden", m !== "audio");
}

// ---------------------------------------------------------------- trim UI

$("trimEnable").addEventListener("change", (e) => {
  $("trimControls").classList.toggle("hidden", !e.target.checked);
});

function updateSliderFill() {
  const max = Number($("rangeStart").max) || 1;
  const a = Number($("rangeStart").value);
  const b = Number($("rangeEnd").value);
  $("sliderFill").style.left = `${(Math.min(a, b) / max) * 100}%`;
  $("sliderFill").style.width = `${(Math.abs(b - a) / max) * 100}%`;
}

$("rangeStart").addEventListener("input", () => {
  let a = Number($("rangeStart").value);
  const b = Number($("rangeEnd").value);
  if (a > b - 1) { a = Math.max(0, b - 1); $("rangeStart").value = a; }
  $("trimStart").value = fmtTime(a);
  updateSliderFill();
});
$("rangeEnd").addEventListener("input", () => {
  const a = Number($("rangeStart").value);
  let b = Number($("rangeEnd").value);
  if (b < a + 1) { b = a + 1; $("rangeEnd").value = b; }
  $("trimEnd").value = fmtTime(b);
  updateSliderFill();
});
$("trimStart").addEventListener("change", () => {
  const v = parseTime($("trimStart").value);
  if (v != null) { $("rangeStart").value = v; updateSliderFill(); }
});
$("trimEnd").addEventListener("change", () => {
  const v = parseTime($("trimEnd").value);
  if (v != null) { $("rangeEnd").value = v; updateSliderFill(); }
});

// ---------------------------------------------------------------- download

$("downloadBtn").addEventListener("click", async () => {
  if (!videoInfo) return;
  const body = {
    url: $("urlInput").value.trim(),
    mode,
    quality: mode === "video" ? $("videoQuality").value : $("audioQuality").value,
    container: $("videoContainer").value,
    precise_cut: $("preciseCut").checked,
    reencode_h264: $("reencodeH264").checked,
  };
  if ($("trimEnable").checked) {
    const s = parseTime($("trimStart").value);
    const e = parseTime($("trimEnd").value);
    if (s != null) body.start = s;
    if (e != null && e > 0) body.end = e;
    if (s != null && e != null && e <= s) {
      alert("End time must be after start time.");
      return;
    }
  }
  const btn = $("downloadBtn");
  btn.disabled = true;
  try {
    const { job_id } = await post("/api/download", body);
    addJobCard(job_id, videoInfo.title);
    pollJob(job_id);
  } catch (e) {
    alert(e.message);
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------- jobs

function addJobCard(jobId, title) {
  $("jobsCard").classList.remove("hidden");
  const div = document.createElement("div");
  div.className = "job";
  div.id = `job-${jobId}`;
  div.innerHTML = `
    <div class="job-head">
      <span class="job-title">${escapeHtml(title || "…")}</span>
      <span class="job-status">queued</span>
    </div>
    <div class="progress"><div style="width:0%"></div></div>
    <div class="job-extra"></div>`;
  $("jobsList").prepend(div);
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function pollJob(jobId) {
  pollTimers[jobId] = setInterval(async () => {
    let job;
    try {
      job = await api(`/api/jobs/${jobId}`);
    } catch { return; }
    const el = $(`job-${jobId}`);
    if (!el) return;
    const statusEl = el.querySelector(".job-status");
    const bar = el.querySelector(".progress > div");
    const extra = el.querySelector(".job-extra");

    if (job.status === "downloading") {
      const speed = job.speed ? `${fmtBytes(job.speed)}/s` : "";
      const eta = job.eta ? `ETA ${fmtTime(job.eta)}` : "";
      statusEl.textContent = `${job.progress || 0}% ${speed} ${eta}`.trim();
      bar.style.width = `${job.progress || 0}%`;
    } else if (job.status === "processing") {
      statusEl.textContent = job.detail || "Processing…";
      bar.style.width = "100%";
    } else if (job.status === "done") {
      clearInterval(pollTimers[jobId]);
      statusEl.textContent = `done · ${fmtBytes(job.size)}`;
      statusEl.className = "job-status done";
      bar.style.width = "100%";
      extra.innerHTML =
        `<a href="/files/${encodeURIComponent(job.file)}" download>💾 ${escapeHtml(job.file)}</a>`;
    } else if (job.status === "error") {
      clearInterval(pollTimers[jobId]);
      statusEl.textContent = "failed";
      statusEl.className = "job-status error";
      extra.innerHTML = `<div class="error">${escapeHtml(job.error || "Unknown error")}</div>`;
    }
  }, 800);
}

// ---------------------------------------------------------------- settings

$("settingsBtn").addEventListener("click", openSettings);
$("closeSettings").addEventListener("click", () => $("settingsModal").classList.add("hidden"));
$("settingsModal").addEventListener("click", (e) => {
  if (e.target === $("settingsModal")) $("settingsModal").classList.add("hidden");
});

async function openSettings() {
  const s = await api("/api/settings");
  $("proxyInput").value = s.proxy || "";
  $("cookiesMode").value = s.cookies_mode || "none";
  $("cookiesBrowser").value = s.cookies_browser || "firefox";
  $("concFrags").value = s.concurrent_fragments || 4;
  $("rateLimit").value = s.rate_limit_kbps || 0;
  $("ytdlpVersion").textContent = `v${s.ytdlp_version}`;
  $("cookiesStatus").textContent = s.has_cookies_file ? "✓ cookies saved" : "no cookies saved";
  renderPotStatus(s.pot_provider, s.node_available);
  toggleCookieBoxes();
  $("settingsModal").classList.remove("hidden");
}

function renderPotStatus(pot, nodeOk) {
  pot = pot || { state: "unknown", detail: "" };
  const pill = $("potStatus");
  const map = {
    healthy:    ["✓ running", "ok"],
    starting:   ["… starting", "warn"],
    docker_down:["✗ Docker off", "err"],
    no_docker:  ["✗ no Docker", "err"],
    unknown:    ["… checking", "warn"],
    error:      ["✗ error", "err"],
  };
  const [label, cls] = map[pot.state] || map.unknown;
  pill.textContent = label + (pot.version ? ` (v${pot.version})` : "");
  pill.className = "pill " + cls;
  $("potDetail").textContent = pot.state === "healthy" ? "" : " " + (pot.detail || "");
  $("potNode").textContent = nodeOk
    ? "Node runtime detected (n-sig challenge solver ready)."
    : "⚠ Node ≥22 not found on PATH — install it so streams aren't throttled.";
}

$("potRetry").addEventListener("click", async () => {
  $("potStatus").textContent = "… retrying";
  $("potStatus").className = "pill warn";
  try {
    const pot = await post("/api/pot-restart", {});
    const s = await api("/api/settings");
    renderPotStatus(pot, s.node_available);
  } catch (e) {
    renderPotStatus({ state: "error", detail: e.message }, true);
  }
});

$("cookiesMode").addEventListener("change", toggleCookieBoxes);
function toggleCookieBoxes() {
  const m = $("cookiesMode").value;
  $("cookiesFileBox").classList.toggle("hidden", m !== "file");
  $("cookiesBrowserBox").classList.toggle("hidden", m !== "browser");
}

$("saveSettings").addEventListener("click", async () => {
  await post("/api/settings", {
    proxy: $("proxyInput").value.trim(),
    cookies_mode: $("cookiesMode").value,
    cookies_browser: $("cookiesBrowser").value,
    concurrent_fragments: Number($("concFrags").value),
    rate_limit_kbps: Number($("rateLimit").value) || 0,
  });
  $("settingsStatus").textContent = "✓ saved";
  setTimeout(() => ($("settingsStatus").textContent = ""), 2000);
});

$("saveCookies").addEventListener("click", async () => {
  try {
    const r = await post("/api/cookies", { text: $("cookiesText").value });
    $("cookiesStatus").textContent = r.has_cookies_file ? "✓ cookies saved" : "no cookies saved";
    $("cookiesText").value = "";
  } catch (e) {
    alert(e.message);
  }
});

$("clearCookies").addEventListener("click", async () => {
  await post("/api/cookies", { text: "" });
  $("cookiesStatus").textContent = "no cookies saved";
});

$("updateYtdlp").addEventListener("click", async () => {
  $("updateStatus").textContent = "updating…";
  try {
    const r = await post("/api/update-ytdlp", {});
    $("updateStatus").textContent = "✓ updated — restart the app to apply";
  } catch (e) {
    $("updateStatus").textContent = `failed: ${e.message}`;
  }
});

// restore running jobs on page load
(async () => {
  try {
    const all = await api("/api/jobs");
    for (const job of all.reverse()) {
      addJobCard(job.id, job.url);
      pollJob(job.id);
    }
  } catch {}
})();
