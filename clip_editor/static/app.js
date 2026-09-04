(() => {
  const ASPECTS = ["9:16", "3:4", "4:5", "1:1", "4:3", "16:9"];

  const $ = (id) => document.getElementById(id);
  const stage = $("stage");
  const vid = $("vid");
  const aud = $("aud");
  const stageEmpty = $("stageEmpty");

  const state = {
    project: null,
    aspect: "9:16",
    panX: 0.5,
    panY: 0.5,
    inS: 0,
    outS: 0,
    duration: 0,
    srcW: 0,
    srcH: 0,
    hasVideo: false,
    hasAudio: false,
    audioDuration: 0,
    audioFit: false,
    audioName: "",
    playing: false,
    drag: null,
  };

  function clamp(n, a, b) {
    return Math.min(b, Math.max(a, n));
  }

  function fmt(t) {
    if (!Number.isFinite(t)) return "0.00";
    return t.toFixed(2);
  }

  function setStatus(msg, kind) {
    const el = $("status");
    el.textContent = msg || "";
    el.className = "status" + (kind ? " " + kind : "");
  }

  function applyPan() {
    vid.style.objectPosition = `${state.panX * 100}% ${state.panY * 100}%`;
    refreshCropInfo();
  }

  let cropTimer = null;
  function refreshCropInfo() {
    if (!state.hasVideo) {
      $("cropInfo").textContent = "";
      return;
    }
    clearTimeout(cropTimer);
    cropTimer = setTimeout(async () => {
      try {
        const data = await api("/api/crop-preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            aspect: state.aspect,
            pan_x: state.panX,
            pan_y: state.panY,
          }),
        });
        const c = data.crop;
        const d = data.dest;
        $("cropInfo").textContent =
          `crop ${c.w}×${c.h} at ${c.x},${c.y}  →  ${d.width}×${d.height}`;
      } catch {
        $("cropInfo").textContent = "";
      }
    }, 80);
  }

  function applyAspect() {
    stage.dataset.aspect = state.aspect;
    document.querySelectorAll(".aspects button").forEach((b) => {
      b.setAttribute("aria-pressed", b.dataset.aspect === state.aspect ? "true" : "false");
    });
  }

  function currentTime() {
    return vid.currentTime || 0;
  }

  function videoEditDur() {
    return Math.max(0, state.outS - state.inS);
  }

  function audioStart() {
    return $("audioFollowsIn").checked ? state.inS : 0;
  }

  function audioUsable() {
    return Math.max(0, state.audioDuration - audioStart());
  }

  function updateFitButton() {
    const btn = $("btnFitAudio");
    const v = videoEditDur();
    const a = audioUsable();
    const longer = state.hasAudio && a > v + 0.05 && v > 0.04;
    btn.disabled = !state.hasAudio;
    btn.setAttribute("aria-pressed", state.audioFit && longer ? "true" : "false");
    if (!state.hasAudio) {
      btn.title = "Add a music track first";
      btn.textContent = "Fit";
      return;
    }
    if (longer) {
      btn.title = `Cut audio to ${fmt(v)}s (from ${fmt(a)}s)`;
      btn.textContent = state.audioFit ? `Fit ${fmt(v)}s` : "Fit";
    } else {
      btn.title = "Audio is already no longer than the video";
      btn.textContent = "Fit";
      if (state.audioFit && a <= v + 0.05) state.audioFit = false;
    }
    if (state.hasAudio && state.audioName) {
      if (state.audioFit && longer) {
        $("audioName").textContent =
          `${state.audioName} · ${fmt(state.audioDuration)}s cut to ${fmt(v)}s`;
      } else {
        $("audioName").textContent = `${state.audioName} · ${fmt(state.audioDuration)}s`;
      }
    }
  }

  function applyTrimBar() {
    const dur = state.duration || 0;
    if (dur <= 0) {
      $("trimRange").style.left = "0";
      $("trimRange").style.right = "0";
      return;
    }
    const left = (state.inS / dur) * 100;
    const right = 100 - (state.outS / dur) * 100;
    $("trimRange").style.left = `${left}%`;
    $("trimRange").style.right = `${Math.max(0, right)}%`;
  }

  function updateClock() {
    $("clock").textContent = `${fmt(currentTime())} / ${fmt(state.duration)}`;
    if (state.duration > 0) {
      $("scrub").value = String(Math.round((currentTime() / state.duration) * 1000));
    }
  }

  function syncAudio() {
    if (!state.hasAudio) return;
    const t = currentTime();
    const start = audioStart();
    const offset = start + (t - state.inS);
    if (Math.abs(aud.currentTime - Math.max(0, offset)) > 0.12) {
      try {
        aud.currentTime = Math.max(0, offset);
      } catch {
        /* ignore seek errors before metadata */
      }
    }
  }

  function pauseAll() {
    vid.pause();
    aud.pause();
    state.playing = false;
    $("btnPlay").textContent = "Play";
  }

  async function playAll() {
    if (!state.hasVideo) return;
    if (currentTime() < state.inS - 0.02 || currentTime() >= state.outS - 0.02) {
      vid.currentTime = state.inS;
    }
    vid.muted = state.hasAudio;
    if (state.hasAudio) {
      aud.playbackRate = 1;
      syncAudio();
      try {
        await aud.play();
      } catch {
        /* autoplay */
      }
    }
    try {
      await vid.play();
    } catch (err) {
      setStatus(String(err), "error");
      return;
    }
    state.playing = true;
    $("btnPlay").textContent = "Pause";
  }

  function onTick() {
    if (!state.hasVideo) return;
    if (currentTime() >= state.outS - 0.01) {
      vid.currentTime = state.outS;
      pauseAll();
    } else if (state.playing && state.hasAudio) {
      syncAudio();
    }
    updateClock();
    if (state.playing) requestAnimationFrame(onTick);
    else updateClock();
  }

  vid.addEventListener("loadeddata", () => {
    if (vid.currentTime < 0.001) {
      try {
        vid.currentTime = 0.04;
      } catch {
        /* ignore */
      }
    }
  });

  vid.addEventListener("timeupdate", () => {
    if (currentTime() >= state.outS - 0.01 && !vid.paused) {
      pauseAll();
    }
    updateClock();
  });

  function panFromDrag(dx, dy) {
    if (!state.srcW || !state.srcH) return;
    const box = stage.getBoundingClientRect();
    const srcA = state.srcW / state.srcH;
    const dstA = box.width / box.height;
    if (srcA > dstA) {
      const scale = box.height / state.srcH;
      const overflow = state.srcW * scale - box.width;
      if (overflow > 1) state.panX = clamp(state.panX - dx / overflow, 0, 1);
    } else if (srcA < dstA) {
      const scale = box.width / state.srcW;
      const overflow = state.srcH * scale - box.height;
      if (overflow > 1) state.panY = clamp(state.panY - dy / overflow, 0, 1);
    }
    applyPan();
  }

  stage.addEventListener("pointerdown", (e) => {
    if (!state.hasVideo) return;
    if (e.button !== 0) return;
    state.drag = { x: e.clientX, y: e.clientY };
    stage.classList.add("is-dragging");
    stage.setPointerCapture(e.pointerId);
  });
  stage.addEventListener("pointermove", (e) => {
    if (!state.drag) return;
    panFromDrag(e.clientX - state.drag.x, e.clientY - state.drag.y);
    state.drag = { x: e.clientX, y: e.clientY };
  });
  stage.addEventListener("pointerup", () => {
    state.drag = null;
    stage.classList.remove("is-dragging");
  });
  stage.addEventListener("pointercancel", () => {
    state.drag = null;
    stage.classList.remove("is-dragging");
  });

  $("btnPlay").addEventListener("click", () => {
    if (state.playing) pauseAll();
    else {
      playAll();
      requestAnimationFrame(onTick);
    }
  });

  $("scrub").addEventListener("input", () => {
    if (!state.duration) return;
    const t = (Number($("scrub").value) / 1000) * state.duration;
    vid.currentTime = t;
    syncAudio();
    updateClock();
  });

  document.addEventListener("keydown", (e) => {
    if (e.code !== "Space") return;
    if (e.target && ["INPUT", "TEXTAREA"].includes(e.target.tagName)) return;
    e.preventDefault();
    $("btnPlay").click();
  });

  function setTrimInputs() {
    $("inS").value = String(state.inS.toFixed(2));
    $("outS").value = String(state.outS.toFixed(2));
    applyTrimBar();
  }

  $("inS").addEventListener("change", () => {
    state.inS = clamp(Number($("inS").value) || 0, 0, Math.max(0, state.outS - 0.05));
    setTrimInputs();
    updateFitButton();
  });
  $("outS").addEventListener("change", () => {
    const v = Number($("outS").value);
    state.outS = clamp(v, state.inS + 0.05, state.duration || v);
    setTrimInputs();
    updateFitButton();
  });
  $("btnSetIn").addEventListener("click", () => {
    state.inS = clamp(currentTime(), 0, Math.max(0, state.outS - 0.05));
    setTrimInputs();
    updateFitButton();
  });
  $("btnSetOut").addEventListener("click", () => {
    state.outS = clamp(currentTime(), state.inS + 0.05, state.duration);
    setTrimInputs();
    updateFitButton();
  });

  function fillAspects() {
    const box = $("aspects");
    box.innerHTML = "";
    for (const a of ASPECTS) {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.aspect = a;
      b.textContent = a;
      b.addEventListener("click", () => {
        state.aspect = a;
        applyAspect();
        suggestOut(state.project || null);
        refreshCropInfo();
      });
      box.appendChild(b);
    }
    applyAspect();
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  function basename(path) {
    if (!path) return "";
    const slash = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
    return path.slice(slash + 1);
  }

  function suggestOut(project) {
    const names = (project && project.suggested_names) || {};
    const path = names[state.aspect] || (project && project.suggested_out) || "";
    if (path) {
      $("exportName").textContent = basename(path);
      $("exportName").title = path;
    } else if (state.hasVideo) {
      const safe = state.aspect.replace(":", "x");
      $("exportName").textContent = `…_${safe}.mp4`;
    } else {
      $("exportName").textContent = "name is assigned on export (.mp4)";
      $("exportName").title = "";
    }
  }

  function applyProject(project) {
    state.project = project;
    const v = project.video;
    const a = project.audio;
    if (v) {
      state.hasVideo = true;
      state.srcW = v.width;
      state.srcH = v.height;
      state.duration = v.duration || 0;
      state.inS = 0;
      state.outS = state.duration;
      $("videoName").textContent = `${v.name}\n${v.width}×${v.height} · ${fmt(v.duration)}s`;
      stage.classList.add("has-video");
      const bust = Date.now();
      vid.poster = `/media/poster?t=${bust}`;
      vid.src = `/media/video?t=${bust}`;
      vid.load();
      $("btnPlay").disabled = false;
      $("scrub").disabled = false;
      $("btnSetIn").disabled = false;
      $("btnSetOut").disabled = false;
      $("btnExport").disabled = false;
      setTrimInputs();
      applyPan();
    }
    if (a) {
      state.hasAudio = true;
      state.audioDuration = a.duration || 0;
      state.audioName = a.name;
      state.audioFit = false;
      $("btnClearAudio").disabled = false;
      aud.playbackRate = 1;
      aud.src = `/media/audio?t=${Date.now()}`;
      aud.load();
      vid.muted = true;
    } else {
      state.hasAudio = false;
      state.audioDuration = 0;
      state.audioName = "";
      state.audioFit = false;
      $("audioName").textContent = "none — keeps the video’s audio if it has one";
      $("btnClearAudio").disabled = true;
      aud.removeAttribute("src");
      aud.load();
      vid.muted = false;
    }
    updateFitButton();
    suggestOut(project);
    updateClock();
    if (state.hasAudio && audioUsable() > videoEditDur() + 0.05) {
      setStatus(
        `Music is ${fmt(audioUsable())}s; video is ${fmt(videoEditDur())}s. Fit cuts the extra.`
      );
    }
  }

  async function pickOrFile(role) {
    setStatus("Opening file picker…");
    try {
      const data = await api("/api/pick", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role }),
      });
      if (data.cancelled) {
        setStatus("");
        return false;
      }
      applyProject(data.project);
      setStatus(`Opened ${data.info.name}`);
      return true;
    } catch (err) {
      setStatus(`Picker unavailable (${err.message}). Using the browser file dialog.`, "");
      return false;
    }
  }

  async function uploadFile(role, file) {
    setStatus(`Loading ${file.name}…`);
    const data = await api(`/api/upload/${role}`, {
      method: "PUT",
      headers: { "X-Filename": file.name },
      body: file,
    });
    applyProject(data.project);
    setStatus(`Opened ${data.info.name}`);
  }

  $("btnVideo").addEventListener("click", async () => {
    const ok = await pickOrFile("video");
    if (!ok) $("fileVideo").click();
  });
  $("btnAudio").addEventListener("click", async () => {
    const ok = await pickOrFile("audio");
    if (!ok) $("fileAudio").click();
  });
  $("fileVideo").addEventListener("change", () => {
    const f = $("fileVideo").files[0];
    if (f) uploadFile("video", f).catch((e) => setStatus(e.message, "error"));
    $("fileVideo").value = "";
  });
  $("fileAudio").addEventListener("change", () => {
    const f = $("fileAudio").files[0];
    if (f) uploadFile("audio", f).catch((e) => setStatus(e.message, "error"));
    $("fileAudio").value = "";
  });

  async function openPath(role) {
    const path = window.prompt("Absolute path on this machine:");
    if (!path) return;
    try {
      const data = await api("/api/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role, path }),
      });
      applyProject(data.project);
      setStatus(`Opened ${data.info.name}`);
    } catch (e) {
      setStatus(e.message, "error");
    }
  }
  $("btnVideoPath").addEventListener("click", () => openPath("video"));
  $("btnAudioPath").addEventListener("click", () => openPath("audio"));
  $("audioFollowsIn").addEventListener("change", () => updateFitButton());
  $("btnFitAudio").addEventListener("click", () => {
    if (!state.hasAudio) return;
    const v = videoEditDur();
    const a = audioUsable();
    if (a <= v + 0.05) {
      setStatus("Audio is already no longer than the video");
      return;
    }
    state.audioFit = true;
    aud.playbackRate = 1;
    updateFitButton();
    setStatus(`Cut audio to ${fmt(v)}s (from ${fmt(a)}s)`);
  });
  $("btnClearAudio").addEventListener("click", async () => {
    try {
      const data = await api("/api/clear-audio", { method: "POST", body: "{}" });
      applyProject(data.project);
      setStatus("Audio cleared");
    } catch (e) {
      setStatus(e.message, "error");
    }
  });

  ["dragenter", "dragover"].forEach((ev) => {
    document.addEventListener(ev, (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    });
  });
  document.addEventListener("drop", (e) => {
    e.preventDefault();
    const files = [...(e.dataTransfer.files || [])];
    if (!files.length) return;
    const vidFile = files.find((f) => f.type.startsWith("video/")) || files.find((f) => /\.(mp4|mov|webm|mkv)$/i.test(f.name));
    const audFile = files.find((f) => f.type.startsWith("audio/")) || files.find((f) => /\.(mp3|wav|m4a|aac|ogg|flac)$/i.test(f.name));
    const jobs = [];
    if (vidFile) jobs.push(uploadFile("video", vidFile));
    if (audFile && audFile !== vidFile) jobs.push(uploadFile("audio", audFile));
    if (!jobs.length && files[0]) jobs.push(uploadFile(state.hasVideo ? "audio" : "video", files[0]));
    Promise.all(jobs).catch((err) => setStatus(err.message, "error"));
  });

  let pollTimer = null;
  async function pollExport() {
    try {
      const st = await api("/api/export/status");
      $("progressBar").style.width = `${Math.round((st.percent || 0) * 100)}%`;
      if (st.state === "running") {
        setStatus(`Encoding… ${Math.round((st.percent || 0) * 100)}%`);
        return;
      }
      clearInterval(pollTimer);
      pollTimer = null;
      $("btnExport").disabled = !state.hasVideo;
      if (st.state === "done") {
        const g = st.gate || {};
        setStatus(
          `Wrote ${st.out}\n${g.vcodec} ${g.width}×${g.height} audio=${g.acodec || "none"} ${fmt(g.duration)}s`,
          "ok"
        );
        $("progressBar").style.width = "100%";
      } else if (st.state === "error") {
        setStatus(st.error || "export failed", "error");
        $("progressBar").style.width = "0%";
      }
    } catch (e) {
      clearInterval(pollTimer);
      pollTimer = null;
      $("btnExport").disabled = !state.hasVideo;
      setStatus(e.message, "error");
    }
  }

  $("btnExport").addEventListener("click", async () => {
    pauseAll();
    $("btnExport").disabled = true;
    $("progressBar").style.width = "0%";
    setStatus("Starting export…");
    try {
      await api("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          aspect: state.aspect,
          pan_x: state.panX,
          pan_y: state.panY,
          in_s: state.inS,
          out_s: state.outS,
          audio_follows_in: $("audioFollowsIn").checked,
          audio_offset: 0,
        }),
      });
      pollTimer = setInterval(pollExport, 400);
      pollExport();
    } catch (e) {
      $("btnExport").disabled = !state.hasVideo;
      setStatus(e.message, "error");
    }
  });

  fillAspects();
  api("/api/project")
    .then((p) => {
      if (p.video) applyProject(p);
    })
    .catch(() => {});
})();
