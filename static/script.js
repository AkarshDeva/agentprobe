(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Build elements with textContent only. Replies come from an UNTRUSTED agent, so never innerHTML.
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  const SHORT = {
    normal: "NORMAL", edge_case: "EDGE", off_topic: "OFF-TOPIC", prompt_injection: "INJECTION",
    unsafe: "UNSAFE", privacy: "PRIVACY", messy_input: "MESSY",
  };
  const label = (name) => SHORT[name] || name.replace(/_/g, "-").toUpperCase();
  const SEV = { low: 0, medium: 1, high: 2, critical: 3 };

  /* =====================================================================
     3D SCENE  (plain canvas, no libraries: our own tiny 3D engine)
     ===================================================================== */
  const Scene = (() => {
    const canvas = $("#stage");
    const hero = $("#rig");
    const ctx = canvas.getContext("2d");
    const C = { paper: "#ece6d6", hazard: "#ffd21f", fail: "#ff3d1f", pass: "#b8ff3d", dim: "#4a4639" };

    let W = 0, H = 0, dpr = 1, t = 0, last = 0;
    let cats = [];
    const projectiles = [], particles = [], rings = [];
    let shake = 0, damage = 0, coreFlash = 0, total = 6, running = false;
    const cam = { yaw: 0, pitch: -0.2 };
    const aim = { yaw: 0, pitch: -0.2 };
    const CAM_Z = 9;

    // --- the agent under test: a spinning icosahedron ---
    const phi = (1 + Math.sqrt(5)) / 2;
    const raw = [[-1, phi, 0], [1, phi, 0], [-1, -phi, 0], [1, -phi, 0], [0, -1, phi], [0, 1, phi],
      [0, -1, -phi], [0, 1, -phi], [phi, 0, -1], [phi, 0, 1], [-phi, 0, -1], [-phi, 0, 1]];
    const norm = Math.hypot(1, phi), RADIUS = 1.5;
    const verts = raw.map((v) => v.map((c) => (c / norm) * RADIUS));
    const edges = [];
    for (let i = 0; i < raw.length; i++) {
      for (let j = i + 1; j < raw.length; j++) {
        const d = Math.hypot(raw[i][0] - raw[j][0], raw[i][1] - raw[j][1], raw[i][2] - raw[j][2]);
        if (Math.abs(d - 2) < 0.01) edges.push([i, j]);
      }
    }
    const stars = Array.from({ length: 90 }, () => {
      const a = Math.random() * 6.283, b = Math.acos(2 * Math.random() - 1), r = 9 + Math.random() * 8;
      return [r * Math.sin(b) * Math.cos(a), r * Math.cos(b), r * Math.sin(b) * Math.sin(a)];
    });
    const OCTA = [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]];
    const OCTA_EDGES = [[0, 2], [0, 3], [0, 4], [0, 5], [1, 2], [1, 3], [1, 4], [1, 5], [2, 4], [2, 5], [3, 4], [3, 5]];

    // --- tiny 3D maths ---
    function spin(p, ay, ax) {
      let [x, y, z] = p;
      let c = Math.cos(ay), s = Math.sin(ay);
      [x, z] = [x * c + z * s, -x * s + z * c];
      c = Math.cos(ax); s = Math.sin(ax);
      [y, z] = [y * c - z * s, y * s + z * c];
      return [x, y, z];
    }
    const view = (p) => spin(p, cam.yaw, cam.pitch);
    function proj(v) {
      const z = v[2] + CAM_Z;
      const k = (H * 1.1) / Math.max(z, 0.2);
      return { x: W / 2 + v[0] * k, y: H / 2 + v[1] * k, k, z };
    }
    const mix = (a, b, f) => a + (b - a) * f;
    function hexMix(h1, h2, f) {
      const n = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
      const [a, b] = [n(h1), n(h2)];
      return `rgb(${a.map((v, i) => Math.round(mix(v, b[i], f))).join(",")})`;
    }

    function resize() {
      const rect = canvas.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      W = Math.max(1, rect.width); H = Math.max(1, rect.height);
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    }

    function satPos(i) {
      const n = Math.max(cats.length, 1);
      const a = (i / n) * 6.283 + t * (calm ? 0.06 : 0.22);
      const ringR = 3.4 * Math.min(1, W / (H * 1.3));
      const x = Math.cos(a) * ringR, z = Math.sin(a) * ringR;
      return [x, Math.sin(a * 2 + i) * 0.35 + z * 0.22, z];
    }

    function satColor(c) {
      if (c.active > 0) return hexMix(C.paper, C.hazard, 0.5 + 0.5 * Math.sin(t * 12));
      if (c.fail > 0) return hexMix(C.paper, C.fail, Math.min(1, 0.45 + c.fail / (c.pass + c.fail)));
      if (c.pass > 0) return C.pass;
      return "#8a8472";
    }

    // --- public actions ---
    function setCategories(names) {
      cats = names.map((name) => ({ name, pass: 0, fail: 0, active: 0, on: true }));
    }
    function setActive(name, on) { const c = cats.find((x) => x.name === name); if (c) c.on = on; }
    function setTotal(n) { total = Math.max(n, 1); }
    function setRunning(v) { running = v; }
    function reset() {
      cats.forEach((c) => { c.pass = 0; c.fail = 0; c.active = 0; });
      projectiles.length = particles.length = rings.length = 0;
      damage = 0; shake = 0; coreFlash = 0;
    }
    function attack(name) {
      const i = cats.findIndex((c) => c.name === name);
      if (i < 0) return;
      cats[i].active += 1;
      projectiles.push({ i, name, t0: t, dur: calm ? 1.2 : 0.85, trail: [] });
    }
    function verdict(name, passed, severity) {
      const c = cats.find((x) => x.name === name);
      if (c) { c.active = Math.max(0, c.active - 1); passed ? c.pass++ : c.fail++; }
      if (passed) {
        rings.push({ t0: t, dur: 1.1, max: 4.4, color: C.pass });
        coreFlash = Math.max(coreFlash, 0.5);
        return;
      }
      const sev = SEV[severity] ?? 1;
      const n = calm ? 8 : [14, 24, 38, 64][sev];
      for (let k = 0; k < n; k++) {
        const a = Math.random() * 6.283, b = Math.acos(2 * Math.random() - 1), sp = 1.6 + Math.random() * 3.6;
        particles.push({
          p: [Math.sin(b) * Math.cos(a) * RADIUS, Math.cos(b) * RADIUS, Math.sin(b) * Math.sin(a) * RADIUS],
          v: [Math.sin(b) * Math.cos(a) * sp, Math.cos(b) * sp, Math.sin(b) * Math.sin(a) * sp],
          life: 1.1 + Math.random() * 1.2, age: 0, color: [C.fail, C.hazard, C.paper][k % 3],
        });
      }
      rings.push({ t0: t, dur: 0.7, max: 3, color: C.fail });
      damage = Math.min(1, damage + 1 / Math.max(total, 6));
      coreFlash = 1;
      if (!calm) shake = Math.min(1, 0.45 + sev * 0.22);
      if (sev >= 2) bigHit();
    }
    function bigHit() {
      if (calm) return;
      const f = $("#flash"); f.classList.remove("on"); void f.offsetWidth; f.classList.add("on");
      hero.classList.remove("hit"); void hero.offsetWidth; hero.classList.add("hit");
    }

    // --- drawing ---
    function line3(a, b, color, width, alpha) {
      const pa = proj(a), pb = proj(b);
      if (pa.z < 0.5 || pb.z < 0.5) return;
      ctx.globalAlpha = alpha; ctx.strokeStyle = color; ctx.lineWidth = width;
      ctx.beginPath(); ctx.moveTo(pa.x, pa.y); ctx.lineTo(pb.x, pb.y); ctx.stroke();
    }

    function drawFloor() {
      const y = 2.7, off = (t * 0.7) % 1;
      for (let z = -9; z <= 13; z++) {
        const zz = z - off;
        line3(view([-13, y, zz]), view([13, y, zz]), "#34312a", 1, Math.max(0, 0.55 - Math.abs(zz) * 0.035));
      }
      for (let x = -13; x <= 13; x += 1.6) {
        line3(view([x, y, -9]), view([x, y, 13]), "#34312a", 1, Math.max(0, 0.5 - Math.abs(x) * 0.03));
      }
      ctx.globalAlpha = 1;
    }

    function drawStars() {
      ctx.fillStyle = C.paper;
      for (const s of stars) {
        const p = proj(view(s));
        if (p.z < 0.5) continue;
        ctx.globalAlpha = 0.35; ctx.fillRect(p.x, p.y, 1.5, 1.5);
      }
      ctx.globalAlpha = 1;
    }

    function drawCore() {
      const color = hexMix(C.paper, C.fail, damage);
      const glow = coreFlash;
      const pts = verts.map((v) => view(spin(v, t * (calm ? 0.1 : 0.45), t * 0.17)));
      ctx.shadowColor = glow > 0.3 ? C.hazard : color;
      ctx.shadowBlur = 6 + glow * 26;
      for (const [i, j] of edges) {
        const a = proj(pts[i]), b = proj(pts[j]);
        ctx.globalAlpha = 0.45 + 0.5 * Math.min(1, (a.k + b.k) / (H * 0.22));
        ctx.strokeStyle = glow > 0.55 ? C.hazard : color;
        ctx.lineWidth = 1.6 + glow * 1.8;
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      }
      ctx.shadowBlur = 0; ctx.fillStyle = color;
      for (const v of pts) { const p = proj(v); ctx.globalAlpha = 1; ctx.fillRect(p.x - 2.5, p.y - 2.5, 5, 5); }
      // inner octahedron spinning the other way
      const inner = OCTA.map((v) => view(spin(v.map((c) => c * 0.62), -t * 0.7, t * 0.3)));
      ctx.strokeStyle = "#6f6a5c"; ctx.lineWidth = 1; ctx.globalAlpha = 0.6;
      for (const [i, j] of OCTA_EDGES) {
        const a = proj(inner[i]), b = proj(inner[j]);
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }

    function drawSat(c, i) {
      const centre = view(satPos(i));
      const s = (c.active > 0 ? 0.42 : 0.3) * (c.on ? 1 : 0.55);
      const pts = OCTA.map((v) => view(spin(v.map((q) => q * s), t * 0.9 + i, t * 0.5)).map((q, k) => q + centre[k]));
      const color = c.on ? satColor(c) : "#4a4639";
      ctx.strokeStyle = color; ctx.lineWidth = c.active > 0 ? 2.4 : 1.6;
      ctx.shadowColor = color; ctx.shadowBlur = c.active > 0 ? 18 : 4;
      const depth = Math.min(1, Math.max(0.35, 1.25 - (centre[2] + 3.5) / 8));
      ctx.globalAlpha = depth;
      for (const [a, b] of OCTA_EDGES) {
        const pa = proj(pts[a]), pb = proj(pts[b]);
        ctx.beginPath(); ctx.moveTo(pa.x, pa.y); ctx.lineTo(pb.x, pb.y); ctx.stroke();
      }
      ctx.shadowBlur = 0;
      const p = proj(centre);
      ctx.fillStyle = color; ctx.font = "700 11px 'JetBrains Mono', monospace"; ctx.textAlign = "center";
      ctx.fillText(label(c.name), p.x, p.y + 30 + p.k * 0.05);
      if (c.pass + c.fail > 0) {
        ctx.font = "400 10px 'JetBrains Mono', monospace"; ctx.globalAlpha = depth * 0.8;
        ctx.fillText(`${c.pass} held  ${c.fail} broke`, p.x, p.y + 44 + p.k * 0.05);
      }
      ctx.globalAlpha = 1;
    }

    function drawProjectiles() {
      for (let k = projectiles.length - 1; k >= 0; k--) {
        const pr = projectiles[k];
        const f = (t - pr.t0) / pr.dur;
        if (f >= 1) {
          rings.push({ t0: t, dur: 0.45, max: 2.3, color: C.hazard });
          coreFlash = Math.max(coreFlash, 0.7);
          projectiles.splice(k, 1);
          continue;
        }
        const from = satPos(pr.i), e = f * f * (3 - 2 * f);
        const pos = [from[0] * (1 - e), from[1] * (1 - e), from[2] * (1 - e)];
        pr.trail.push(pos); if (pr.trail.length > 9) pr.trail.shift();
        ctx.fillStyle = C.hazard; ctx.shadowColor = C.hazard; ctx.shadowBlur = 16;
        pr.trail.forEach((q, n) => {
          const p = proj(view(q));
          ctx.globalAlpha = (n + 1) / pr.trail.length;
          const sz = 2 + (n / pr.trail.length) * 5;
          ctx.fillRect(p.x - sz / 2, p.y - sz / 2, sz, sz);
        });
        ctx.shadowBlur = 0; ctx.globalAlpha = 1;
      }
    }

    function drawRings() {
      for (let k = rings.length - 1; k >= 0; k--) {
        const r = rings[k], f = (t - r.t0) / r.dur;
        if (f >= 1) { rings.splice(k, 1); continue; }
        const rad = r.max * (1 - Math.pow(1 - f, 3));
        ctx.strokeStyle = r.color; ctx.lineWidth = 3 * (1 - f) + 0.5; ctx.shadowColor = r.color; ctx.shadowBlur = 12;
        ctx.globalAlpha = 1 - f; ctx.beginPath();
        for (let s = 0; s <= 48; s++) {
          const a = (s / 48) * 6.283, p = proj(view([Math.cos(a) * rad, 0.15, Math.sin(a) * rad]));
          s ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y);
        }
        ctx.stroke(); ctx.shadowBlur = 0; ctx.globalAlpha = 1;
      }
    }

    function drawParticles(dt) {
      for (let k = particles.length - 1; k >= 0; k--) {
        const q = particles[k];
        q.age += dt;
        if (q.age >= q.life) { particles.splice(k, 1); continue; }
        q.v[1] += 2.2 * dt;
        for (let a = 0; a < 3; a++) q.p[a] += q.v[a] * dt;
        const p0 = proj(view(q.p)), p1 = proj(view(q.p.map((c, a) => c - q.v[a] * 0.06)));
        ctx.globalAlpha = 1 - q.age / q.life; ctx.strokeStyle = q.color; ctx.lineWidth = 2.2;
        ctx.beginPath(); ctx.moveTo(p0.x, p0.y); ctx.lineTo(p1.x, p1.y); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }

    function frame(now) {
      const dt = Math.min(0.05, (now - last) / 1000 || 0.016);
      last = now; t += dt;
      cam.yaw += (aim.yaw + Math.sin(t * 0.2) * 0.08 - cam.yaw) * 0.05;
      cam.pitch += (aim.pitch - cam.pitch) * 0.05;
      coreFlash = Math.max(0, coreFlash - dt * 1.8);
      shake = Math.max(0, shake - dt * 2.6);

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      if (shake > 0) ctx.translate((Math.random() - 0.5) * 16 * shake, (Math.random() - 0.5) * 16 * shake);
      ctx.lineCap = "round";

      drawStars();
      drawFloor();
      // draw far things first so near things sit on top
      const items = cats.map((c, i) => ({ z: view(satPos(i))[2], draw: () => drawSat(c, i) }));
      items.push({ z: 0, draw: drawCore });
      items.sort((a, b) => b.z - a.z);
      items.forEach((it) => it.draw());
      drawRings();
      drawProjectiles();
      drawParticles(dt);

      if (!running && !calm && Math.random() < dt * 0.25) {
        // idle: a lazy test attack now and then so the rig feels alive
        const c = cats[Math.floor(Math.random() * cats.length)];
        if (c) projectiles.push({ i: cats.indexOf(c), name: c.name, t0: t, dur: 1.3, trail: [] });
      }
      requestAnimationFrame(frame);
    }

    hero.addEventListener("pointermove", (e) => {
      const r = hero.getBoundingClientRect();
      aim.yaw = ((e.clientX - r.left) / r.width - 0.5) * 0.8;
      aim.pitch = -0.2 + ((e.clientY - r.top) / r.height - 0.5) * 0.35;
    });
    new ResizeObserver(resize).observe(canvas);
    resize();
    requestAnimationFrame(frame);

    return { setCategories, setActive, setTotal, setRunning, reset, attack, verdict };
  })();

  /* =====================================================================
     APP: form, live run, report
     ===================================================================== */
  const refs = {
    form: $("#rigForm"), url: $("#targetUrl"), chips: $("#chips"), fire: $("#fireBtn"), fireText: $("#fireText"),
    err: $("#errBox"), feed: $("#feed"), meter: $("#meterFill"), stage: $("#stageName"), stageSub: $("#stageSub"),
    fired: $("#cFired"), pass: $("#cPass"), fail: $("#cFail"), dot: $("#statusDot"), statusText: $("#statusText"),
    results: $("#results"),
  };
  const S = { config: null, per: 1, rounds: 2, picked: new Set(), runId: null, cursor: 0, total: 0, fired: 0, pass: 0, fail: 0, resolved: 0, running: false, lastSummary: "" };
  const pad = (n) => String(n).padStart(2, "0");

  function setStage(name, sub) {
    refs.stage.textContent = name;
    refs.stage.classList.remove("swap"); void refs.stage.offsetWidth; refs.stage.classList.add("swap");
    if (sub !== undefined) refs.stageSub.textContent = sub;
  }
  function log(cls, text) {
    const li = el("li", cls, text);
    refs.feed.appendChild(li);
    while (refs.feed.children.length > 80) refs.feed.removeChild(refs.feed.firstChild);
    refs.feed.scrollTop = refs.feed.scrollHeight;
  }
  function paintCounters() {
    refs.fired.textContent = pad(S.fired); refs.pass.textContent = pad(S.pass); refs.fail.textContent = pad(S.fail);
    refs.meter.style.width = `${S.total ? Math.min(100, (S.resolved / S.total) * 100) : 0}%`;
  }
  function showError(msg) { refs.err.textContent = msg; refs.err.hidden = false; }
  function setRunning(on) {
    S.running = on;
    Scene.setRunning(on);
    refs.fire.disabled = on;
    refs.fireText.textContent = on ? "FIRING..." : "FIRE";
    refs.dot.classList.toggle("run", on);
    refs.statusText.textContent = on ? "RUN IN PROGRESS" : "LAB ONLINE";
  }

  function handle(ev) {
    switch (ev.kind) {
      case "stage":
        setStage(ev.name, `Round ${ev.round}`);
        log("sys", `${ev.name}  (round ${ev.round})`);
        break;
      case "plan":
        S.total += ev.tests.length; Scene.setTotal(S.total);
        log("sys", `${ev.tests.length} tests queued`);
        break;
      case "attack":
        S.fired++; Scene.attack(ev.category);
        log("fire-l", `FIRE  ${ev.id}`);
        break;
      case "reply":
        log("reply", `  reply ${ev.id}  ${ev.status ?? "NONE"}  ${ev.latency ?? 0}s`);
        break;
      case "verdict":
        S.resolved++; ev.passed ? S.pass++ : S.fail++;
        Scene.verdict(ev.category, ev.passed, ev.severity);
        log(ev.passed ? "ok" : "bad", `${ev.passed ? "HELD " : "BROKE"} ${ev.id}  ${ev.passed ? "" : ev.severity.toUpperCase() + " "}(${ev.judged_by})`);
        break;
      case "supervisor":
        log("sup", ev.note);
        break;
      case "error":
        showError(ev.message);
        log("bad", `ERROR: ${ev.message}`);
        break;
      default: break;
    }
    paintCounters();
  }

  async function poll(retries = 0) {
    if (!S.runId) return;
    try {
      const res = await fetch(`/api/runs/${S.runId}?since=${S.cursor}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      data.events.forEach(handle);
      S.cursor = data.next;
      if (data.status === "running") { setTimeout(poll, 900); return; }
      setRunning(false);
      if (data.status === "done") finish(data.result);
      else { setStage("RUN FAILED", data.error || ""); }
    } catch (e) {
      if (retries < 5) { setTimeout(() => poll(retries + 1), 1500); return; }
      setRunning(false); showError("Lost contact with the lab server.");
    }
  }

  async function start(e) {
    e.preventDefault();
    refs.err.hidden = true;
    const categories = [...S.picked];
    if (!categories.length) { showError("Pick at least one attack type."); return; }

    let res, data;
    try {
      res = await fetch("/api/runs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_url: refs.url.value.trim(), per_category: S.per, rounds: S.rounds, categories }),
      });
      data = await res.json();
    } catch (err) { showError("Could not reach the lab server."); return; }
    if (!res.ok) { showError(data.error || "The run could not start."); return; }

    Object.assign(S, { runId: data.run_id, cursor: 0, total: 0, fired: 0, pass: 0, fail: 0, resolved: 0 });
    Scene.reset(); paintCounters();
    refs.feed.replaceChildren(el("li", "sys", "Run started."));
    refs.results.hidden = true;
    setRunning(true); setStage("ARMING", "Writing the first attacks");
    poll();
  }

  /* ---------- the report ---------- */
  function countUp(node, target, ms) {
    if (calm) { node.textContent = String(Math.round(target)); return; }
    const t0 = performance.now();
    (function step(now) {
      const f = Math.min(1, (now - t0) / ms);
      node.textContent = String(Math.round(target * (1 - Math.pow(1 - f, 4))));
      if (f < 1) requestAnimationFrame(step);
    })(t0);
  }

  function fixLine(text) {
    const li = el("li");
    text.split(/\*\*(.+?)\*\*/g).forEach((part, i) => li.appendChild(i % 2 ? el("strong", "", part) : document.createTextNode(part)));
    return li;
  }

  function finish(result) {
    const s = result.summary;
    refs.results.hidden = false;

    // big number
    const core = s.core, box = $("#scoreBox"), num = $("#scoreNum");
    box.className = "score-num " + (core.pass_rate === null ? "mid" : core.pass_rate >= 80 ? "high" : core.pass_rate >= 50 ? "mid" : "low");
    if (core.pass_rate === null) { num.textContent = "N/A"; $("#scoreUnit").textContent = ""; }
    else { $("#scoreUnit").textContent = "%"; countUp(num, core.pass_rate, 1400); }
    $("#scoreSub").textContent = `${core.passed} of ${core.graded} round-one tests held. Use this number to compare runs.`;

    const stat = (k, v) => { const d = el("div"); d.append(el("dt", "", k), el("dd", "", v)); return d; };
    $("#stats").replaceChildren(
      stat("ALL TESTS", `${s.passed}/${s.graded}`), stat("DEEP-PROBE", String(s.deep_probe_tests)),
      stat("NOT GRADED", String(s.ungraded)), stat("AVG REPLY", s.average_latency === null ? "n/a" : `${s.average_latency}s`),
    );

    // bars
    const bars = $("#bars"); bars.classList.remove("in");
    bars.replaceChildren(...Object.entries(s.by_category).map(([name, d]) => {
      const li = el("li"), track = el("div", "track");
      const g = d.graded || 1;
      const p = el("span", "seg p"), f = el("span", "seg f");
      p.style.setProperty("--w", `${(d.passed / g) * 100}%`); f.style.setProperty("--w", `${(d.failed / g) * 100}%`);
      track.append(p, f);
      li.append(el("span", "name", label(name)), track, el("span", "num", d.pass_rate === null ? "n/a" : `${d.passed}/${d.graded}  ${d.pass_rate}%`));
      return li;
    }));

    // incident cards
    const bad = result.results.filter((r) => !r.passed && r.judged_by !== "none")
      .sort((a, b) => SEV[b.severity] - SEV[a.severity] || a.score - b.score).slice(0, 9);
    const cards = $("#cards");
    if (!bad.length) {
      const box2 = el("div", "clear", "NO INCIDENTS. Every graded test held.");
      box2.appendChild(el("small", "", "That does not prove the agent is safe. It only means these tests passed."));
      cards.replaceChildren(box2);
    } else {
      cards.replaceChildren(...bad.map((r) => {
        const card = el("article", "card"), inner = el("div", "card-in");
        card.appendChild(el("span", `stamp ${r.severity}`, r.severity.toUpperCase()));
        const q = el("blockquote"); const k1 = el("span", "k", "ATTACK"); q.append(k1, document.createTextNode(r.message));
        const rep = el("div"); rep.append(el("span", "k", `REPLY  (HTTP ${r.status ?? "none"})`), el("div", "reply-t", r.reply || "(empty)"));
        inner.append(el("div", "cat", `${label(r.category)}  /  ${r.id}`), q, rep, el("p", "why", r.reason), el("div", "by", `GRADED BY ${r.judged_by.toUpperCase()}  |  SCORE ${r.score}/10`));
        card.appendChild(inner);
        if (!calm) {
          card.addEventListener("pointermove", (e) => {
            const b = card.getBoundingClientRect();
            const x = (e.clientX - b.left) / b.width - 0.5, y = (e.clientY - b.top) / b.height - 0.5;
            card.style.transform = `rotateY(${x * 14}deg) rotateX(${-y * 14}deg) translateZ(10px)`;
          });
          card.addEventListener("pointerleave", () => { card.style.transform = ""; });
        }
        return card;
      }));
    }

    $("#timeline").replaceChildren(...(result.notes.length ? result.notes : ["No supervisor decisions were recorded."]).map((n) => el("li", "", n)));
    const fixes = result.suggestions.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => l.replace(/^[-*]\s*/, ""));
    $("#fixes").replaceChildren(...fixes.map(fixLine));

    $("#dlBtn").href = `/api/runs/${S.runId}/report.md`;
    S.lastSummary = `AgentProbe: core suite ${core.passed}/${core.graded} (${core.pass_rate ?? "n/a"}%). All tests ${s.passed}/${s.graded}. Failed: ${s.failed}.`;

    const label2 = core.pass_rate === null ? "REPORT READY" : core.pass_rate >= 80 ? "IT HELD" : core.pass_rate >= 50 ? "CRACKS FOUND" : "IT BROKE";
    setStage(label2, "Scroll down for the damage report");
    requestAnimationFrame(() => requestAnimationFrame(() => bars.classList.add("in")));
    setTimeout(() => refs.results.scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "start" }), 900);
  }

  /* ---------- setup ---------- */
  function wireSteppers(limits) {
    document.querySelectorAll(".stepper").forEach((box) => {
      const key = box.dataset.key, out = $("output", box), max = key === "per" ? limits.per_category : limits.rounds;
      box.addEventListener("click", (e) => {
        const b = e.target.closest("button"); if (!b) return;
        S[key] = Math.max(1, Math.min(max, S[key] + Number(b.dataset.d)));
        out.textContent = S[key];
      });
    });
  }

  async function init() {
    try {
      S.config = await (await fetch("/api/config")).json();
    } catch (e) { showError("Could not load the lab settings."); return; }
    const { categories, default_target: dt, limits } = S.config;
    refs.url.value = dt;
    const names = Object.keys(categories);
    Scene.setCategories(names);
    names.forEach((n) => S.picked.add(n));
    refs.chips.replaceChildren(...names.map((n) => {
      const b = el("button", "chip", label(n)); b.type = "button"; b.setAttribute("aria-pressed", "true"); b.title = categories[n];
      b.addEventListener("click", () => {
        const on = b.getAttribute("aria-pressed") !== "true";
        if (!on && S.picked.size === 1) return;
        b.setAttribute("aria-pressed", String(on)); on ? S.picked.add(n) : S.picked.delete(n); Scene.setActive(n, on);
      });
      return b;
    }));
    wireSteppers(limits);
    refs.form.addEventListener("submit", start);
    $("#againBtn").addEventListener("click", () => { $("#rig").scrollIntoView({ behavior: calm ? "auto" : "smooth" }); setStage("STANDBY", "Pick your weapons. Press fire."); });
    $("#copyBtn").addEventListener("click", async (e) => {
      try { await navigator.clipboard.writeText(S.lastSummary); e.target.textContent = "COPIED"; }
      catch (err) { e.target.textContent = "COPY FAILED"; }
      setTimeout(() => { e.target.textContent = "COPY SUMMARY"; }, 1600);
    });
  }
  init();
})();