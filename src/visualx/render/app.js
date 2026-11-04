/* VisualX_AI page script (ARCHITECTURE AD-09, FR-40 to FR-50). GPL-3.0-or-later.
 *
 * Security rules for this file (threat T1, SR-02, SR-07):
 * - Every value from X goes into the DOM with textContent or as a DOM property.
 *   This file never uses an HTML-parsing setter or method (the inner/outer HTML
 *   properties, insertAdjacent HTML, or document write). A unit test checks this.
 * - A URL from X becomes a link only when its scheme is http: or https:.
 * - Colors come only from the payload "cfg" block (configuration), checked
 *   against #RRGGBB before use, or from the constant palettes in this file.
 *   No value from X goes into a style or a class name. The only attributes
 *   with a value from X are href (checked as above) and the user IDs in the
 *   data-focus and data-path test hooks (digits or h:<handle>, as text).
 * - All Sigma API calls are in the SIGMA ADAPTER section, so that a Sigma
 *   version change touches one section (AD-09).
 * - No worker, no blob: URL, no network. The layout runs in
 *   requestAnimationFrame chunks on the main thread (CSP worker-src 'none').
 */
(function () {
  "use strict";

  var HEX = /^#[0-9A-Fa-f]{6}$/;
  var MID = "·";  // middle dot
  var COMMUNITY_COLORS = ["#4FC3B5", "#E8A33D", "#7FA7E8", "#D96C8C", "#9BD06A", "#B98AE0",
    "#E6D45A", "#5FB0D8", "#E07B54", "#8FD3A8", "#C9A27A", "#A0A8B8"];
  function safeColor(c, fallback) { return HEX.test(String(c)) ? String(c) : (fallback || "#808080"); }
  function rgbOf(hex) {
    var h = safeColor(hex);
    return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
  }
  function rgba(hex, a) { var c = rgbOf(hex); return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")"; }
  function $(id) { return document.getElementById(id); }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function el(tag, text, cls) {
    var e = document.createElement(tag);
    if (text !== undefined && text !== null) { e.textContent = String(text); }
    if (cls) { e.className = cls; }
    return e;
  }
  function num(v) { return typeof v === "number" && isFinite(v) ? v : 0; }
  function fmtInt(v) { return Math.round(num(v)).toLocaleString("en-US"); }
  function fmt(v) {
    if (v === null || v === undefined || v === "") { return "not in data"; }
    if (typeof v === "number") { return Number.isInteger(v) ? v.toLocaleString("en-US") : String(Math.round(v * 10000) / 10000); }
    return String(v);
  }
  function compact(v) {
    if (typeof v !== "number" || !isFinite(v)) { return "-"; }
    if (Math.abs(v) >= 1e6) { return (Math.round(v / 1e5) / 10) + "M"; }
    if (Math.abs(v) >= 1e4) { return Math.round(v / 1e3) + "k"; }
    if (Math.abs(v) >= 1e3) { return (Math.round(v / 1e2) / 10) + "k"; }
    return Number.isInteger(v) ? String(v) : String(Math.round(v * 10) / 10);
  }
  function showError(msg) {
    var box = $("vx-error");
    box.textContent = msg;
    box.style.display = "block";
    document.body.setAttribute("data-vx-error", "1");
  }
  function cssVar(name) { return getComputedStyle(document.body).getPropertyValue(name).trim(); }

  /* ------------------------------------------------------------------ */
  /* SIGMA ADAPTER: the only section that calls the Sigma API.           */
  /* ------------------------------------------------------------------ */
  // A sphere look for nodes: one node layer that shades the fill color with a
  // light from the top left, a specular spot, and a soft rim (GLSL, no texture).
  var SPHERE_GLSL = [
    "vec4 layer_sphere(vec4 c) {",
    "  vec2 p = context.uv / max(context.shapeSize, 0.0001);",
    "  float r2 = clamp(dot(p, p), 0.0, 1.0);",
    "  vec3 n = vec3(p.x, p.y, sqrt(1.0 - r2));",
    "  vec3 L = normalize(vec3(-0.45, 0.55, 0.8));",
    "  float diff = clamp(dot(n, L), 0.0, 1.0);",
    "  float spec = pow(clamp(dot(n, normalize(L + vec3(0.0, 0.0, 1.0))), 0.0, 1.0), 30.0);",
    "  float rim = smoothstep(0.82, 1.0, sqrt(r2));",
    "  vec3 rgb = c.rgb * (0.42 + 0.7 * diff) + vec3(0.6 * spec) + c.rgb * 0.35 * rim;",
    "  return vec4(min(rgb, vec3(1.0)), c.a);",
    "}"
  ].join("\n");

  var SigmaView = {
    r: null,
    create: function (container, graph, styles) {
      var lib = window.Sigma;
      var Ctor = lib && lib.Sigma ? lib.Sigma : lib;
      var P = lib.primitives, R = lib.rendering;
      var nodePrims = {}, edgePrims = {}, k;
      for (k in P.DEFAULT_NODE_PRIMITIVES) { nodePrims[k] = P.DEFAULT_NODE_PRIMITIVES[k]; }
      for (k in P.DEFAULT_EDGE_PRIMITIVES) { edgePrims[k] = P.DEFAULT_EDGE_PRIMITIVES[k]; }
      nodePrims.layers = [{
        name: "sphere", glsl: SPHERE_GLSL, uniforms: [],
        attributes: [{ name: "sphereColor", size: 4, type: WebGL2RenderingContext.UNSIGNED_BYTE, normalized: true, source: "color" }]
      }];
      nodePrims.label = { font: { family: "Segoe UI, system-ui, sans-serif" } };
      edgePrims.extremities = [R.extremityArrow({ lengthRatio: 4, widthRatio: 3.2 })];
      this.r = new Ctor(graph, container, {
        primitives: { nodes: nodePrims, edges: edgePrims },
        settings: {
          renderEdgeLabels: false,
          enableEdgeEvents: false,
          enableNodeDrag: true,
          enableCameraRotation: false,
          enableCameraMouseRotation: false,
          labelRenderedSizeThreshold: 4,
          labelDensity: 1,
          minEdgeThickness: 0.5,
          hideEdgesOnMove: false,
          itemSizesReference: "screen",
          stagePadding: 40,
          allowInvalidContainer: true,  // the map is hidden (size 0) while the Overview shows
          zoomDuration: 200
        },
        styles: styles
      });
      return this;
    },
    setSetting: function (k, v) { this.r.setSetting(k, v); },
    on: function (ev, fn) { this.r.on(ev, fn); },
    nodeToViewport: function (x, y) { return this.r.graphToViewport({ x: x, y: y }); },
    focusNode: function (key) {
      var d = this.r.getNodeDisplayData(key);
      if (!d) { return; }
      this.r.getCamera().animate({ x: d.x, y: d.y, ratio: 0.35 }, { duration: 450 });
    },
    fitNodes: function (keys) {
      var xs = [], ys = [], self = this;
      keys.forEach(function (k) { var d = self.r.getNodeDisplayData(k); if (d) { xs.push(d.x); ys.push(d.y); } });
      if (!xs.length) { return; }
      var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
      var minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
      var ratio = Math.max(maxX - minX, maxY - minY) * 1.6;
      ratio = Math.min(1.2, Math.max(0.12, ratio));
      this.r.getCamera().animate({ x: (minX + maxX) / 2, y: (minY + maxY) / 2, ratio: ratio }, { duration: 500 });
    },
    zoom: function (factor) {
      var cam = this.r.getCamera();
      cam.animate({ ratio: cam.getState().ratio * factor }, { duration: 220 });
    },
    fit: function () { this.r.getCamera().animate({ x: 0.5, y: 0.5, ratio: 1, angle: 0 }, { duration: 350 }); },
    animateTo: function (targets, done) {
      window.Sigma.utils.animateNodes(this.r.getGraph(), targets, { duration: 650 }, done);
    },
    resize: function () { this.r.resize(true); this.r.refresh(); },
    refresh: function () { this.r.refresh(); },
    // Styles only: positions and sizes in the index do not change (hover, focus marks).
    refreshStyles: function () { this.r.refresh({ skipIndexation: true }); }
  };
  /* ------------------------- end SIGMA ADAPTER ------------------------ */

  async function decodePayload() {
    var b64 = $("vx-payload").textContent.replace(/\s+/g, "");
    var bin = atob(b64);
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) { bytes[i] = bin.charCodeAt(i); }
    var stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
    var text = await new Response(stream).text();
    return JSON.parse(text);
  }

  /* Hex grid and vignette, drawn on a 2D canvas behind the WebGL canvas. */
  function drawBackground() {
    var cv = $("vx-bg"), stage = $("vx-stage");
    var w = stage.clientWidth, h = stage.clientHeight, dpr = window.devicePixelRatio || 1;
    if (!w || !h) { return; }
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    var ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = cssVar("--bg");
    ctx.fillRect(0, 0, w, h);
    var glow = ctx.createRadialGradient(w * 0.5, h * 0.48, 0, w * 0.5, h * 0.48, Math.max(w, h) * 0.6);
    glow.addColorStop(0, cssVar("--glow"));
    glow.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, w, h);
    var s = 22, hw = Math.sqrt(3) * s, row = 1.5 * s;
    ctx.strokeStyle = cssVar("--hex");
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (var ry = -1, y = -s; y < h + s; ry++, y += row) {
      for (var x = (ry % 2 ? hw / 2 : 0) - hw; x < w + hw; x += hw) {
        for (var k = 0; k < 6; k++) {
          var a = Math.PI / 180 * (60 * k - 30);
          var px = x + s * Math.cos(a), py = y + s * Math.sin(a);
          if (k === 0) { ctx.moveTo(px, py); } else { ctx.lineTo(px, py); }
        }
        ctx.closePath();
      }
    }
    ctx.stroke();
    var vg = ctx.createRadialGradient(w / 2, h / 2, Math.min(w, h) * 0.35, w / 2, h / 2, Math.max(w, h) * 0.75);
    vg.addColorStop(0, "rgba(0,0,0,0)");
    vg.addColorStop(1, cssVar("--vignette"));
    ctx.fillStyle = vg;
    ctx.fillRect(0, 0, w, h);
  }

  function App(D) {
    var cfg = D.cfg, N = D.n, P = D.p, E = D.e, F = D.f || [];
    var n = N.id.length, m = E.s.length;
    var states = cfg.states, types = cfg.types;
    var ego = D.meta.ego_index;
    var t0 = D.meta.t0;
    var TYPE_LABEL = { reply: "Replies", quote: "Quotes", mention: "Mentions", repost: "Reposts" };
    var TYPE_ONE = { reply: "reply", quote: "quote", mention: "mention", repost: "repost" };

    // ---- per-node and per-edge derived values ----
    var stateName = new Array(n), missing = new Array(n);
    var i, j, k;
    for (i = 0; i < n; i++) {
      stateName[i] = states[N.st[i]] || "not_scored";
      missing[i] = N.ps[i] === "missing";
    }
    var rulesOf = {};
    D.r.forEach(function (r) { (rulesOf[r[0]] = rulesOf[r[0]] || []).push(r); });
    var findingsOf = {};
    F.forEach(function (f, fi) { (f.m || []).forEach(function (x) { (findingsOf[x] = findingsOf[x] || []).push(fi); }); });

    var counts = [E.cr, E.cq, E.cm, E.cp];
    var edgeType = new Array(m);
    var ft = E.ft || [], lt = E.lt || [];
    var tMax = 0, untimed = 0;
    var nodeFirst = new Array(n).fill(null), nodeLast = new Array(n).fill(null);
    var nodeTypeCount = [];
    for (i = 0; i < n; i++) { nodeTypeCount.push([0, 0, 0, 0]); }
    for (j = 0; j < m; j++) {
      var best = -1, bi = 0;
      for (k = 0; k < types.length; k++) {
        var c = counts[k][j] || 0;
        if (c > best) { best = c; bi = k; }
        nodeTypeCount[E.s[j]][k] += c; nodeTypeCount[E.t[j]][k] += c;
      }
      edgeType[j] = bi;
      var f0 = ft[j], l0 = lt[j];
      if (f0 === null || f0 === undefined) { untimed++; }
      else {
        if (l0 > tMax) { tMax = l0; }
        [E.s[j], E.t[j]].forEach(function (x) {
          if (nodeFirst[x] === null || f0 < nodeFirst[x]) { nodeFirst[x] = f0; }
          if (nodeLast[x] === null || l0 > nodeLast[x]) { nodeLast[x] = l0; }
        });
      }
    }
    var hasTimes = t0 !== null && t0 !== undefined && m > untimed;
    function markOf(i) { return cfg.state_marks[stateName[i]]; }
    function nameOf(i) {
      if (N.h[i]) { return "@" + N.h[i]; }
      if (N.nm[i]) { return N.nm[i]; }
      return "id " + N.id[i];
    }
    function labelOf(i) {
      var s = stateName[i];
      return (s === "some_signals" || s === "many_signals" ? markOf(i) + " " : "") + nameOf(i);
    }
    function timeText(min, withTime) {
      if (min === null || min === undefined || !hasTimes) { return "not in data"; }
      var d = new Date((t0 + min * 60) * 1000);
      var day = d.toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short", year: "numeric" });
      return withTime ? day + ", " + d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : day;
    }
    var genTime = Date.parse(D.meta.generated_at) || Date.now();
    function ageYears(i) {
      var c = P.created_at[i];
      var t = c ? Date.parse(c) : NaN;
      return isFinite(t) ? (genTime - t) / (365.25 * 864e5) : null;
    }

    // ---- view state ----
    var coreMax = cfg.core_view_max_nodes;
    var coreSet = null;
    if (n > coreMax) {
      var order = [];
      for (i = 0; i < n; i++) { if (i !== ego && !missing[i]) { order.push(i); } }
      order.sort(function (a, b) { return (N.wd[b] || 0) - (N.wd[a] || 0) || a - b; });
      coreSet = new Set(order.slice(0, Math.max(0, coreMax - (ego >= 0 ? 1 : 0))));
      if (ego >= 0) { coreSet.add(ego); }
    }
    var view = {
      states: new Set(states), types: new Set(types), minW: cfg.min_tie_weight || 0,
      showMissing: false, core: coreSet !== null, sizeMode: cfg.size_mode, window: null,
      colorMode: "state", nodeScale: 1, edgeAlpha: 0.35, edgeWidth: 1, labels: true, arrows: false,
      community: "all", topic: "all", hubsOnly: false, change: "all"
    };
    var pb = { open: false, playing: false, T: tMax, last: 0, speed: 60 };
    var nodeShown = new Array(n), edgeShown = new Array(m);
    var selected = -1, pathA = -1, pathB = -1, pathNodes = null;
    var hover = -1, keep = null, focusSet = null, focusFinding = -1;
    var community = null;
    // Leiden communities from the analyze stage (FR-26, FR-27). The number is the rank by size.
    var CM = N.cm || [], CS = N.cs || [], HB = N.hb || [], BR = N.br || [], BC = N.bc || [];
    var TC = N.tc || [], TI = N.ti || [];
    var topicNames = (cfg.topics || []).slice();
    var groupsMeta = D.meta.groups || {};
    (function () {
      var sizes = [];
      for (var q = 0; q < n; q++) {
        var cq = CM[q];
        if (typeof cq === "number" && cq >= 0) { sizes[cq] = (sizes[cq] || 0) + 1; }
      }
      for (var r = 0; r < sizes.length; r++) { sizes[r] = sizes[r] || 0; }
      community = sizes.length ? { sizes: sizes } : null;
    })();
    function communityOf(i) { var c = CM[i]; return typeof c === "number" && c >= 0 ? c : -1; }
    function topicOf(i) {
      var t = TC[i];
      if (t && t.length) { return t[0]; }
      return TI[i] || null;
    }
    function hasTopic(i, t) { return (TC[i] && TC[i].indexOf(t) >= 0) || TI[i] === t; }
    // Changes since the earlier snapshot (snapshot.py). CH is null with no earlier snapshot.
    var CH = D.meta.changes || null;
    var NOT_SCORED = states.indexOf("not_scored");
    var CN = N.cn || [], CPS = N.cps || [], CMV = N.cmv || [], CTW = N.ctw || [], CRA = N.cra || [];
    function changeOf(i, kind) {
      if (kind === "new") { return CN[i] === 1; }
      if (kind === "state") { return CPS[i] !== null && CPS[i] !== undefined; }
      // "More signals": a higher signal state. not_scored -> no_signals is not more
      // signals: the account was not scored before (review 20261103, C-10).
      if (kind === "up") { return CPS[i] !== null && CPS[i] !== undefined && CPS[i] !== NOT_SCORED && N.st[i] > CPS[i]; }
      if (kind === "moved") { return CMV[i] === 1; }
      if (kind === "tie") { return typeof CTW[i] === "number" && CTW[i] > 0; }
      if (kind === "rules") { return !!(CRA[i] && CRA[i].length); }
      return changeOf(i, "new") || changeOf(i, "state") || changeOf(i, "moved") || changeOf(i, "tie") || changeOf(i, "rules");
    }
    function shortDate(s) { return typeof s === "string" ? s.slice(0, 16).replace("T", " ") : "an earlier run"; }

    // ---- graphology graph ----
    var G = new graphology.Graph({ type: "directed", multi: false, allowSelfLoops: false });
    for (i = 0; i < n; i++) {
      G.addNode(String(i), {
        ix: i, x: N.x[i], y: N.y[i], size: 3, label: labelOf(i), c: "#808080", cd: "#808080",
        color: "#808080", hidden: false, hl: false, sel: false, ego: i === ego
      });
    }
    for (j = 0; j < m; j++) {
      if (E.s[j] === E.t[j]) { continue; }
      G.addEdgeWithKey("e" + j, String(E.s[j]), String(E.t[j]), {
        si: E.s[j], ti: E.t[j], ty: edgeType[j], weight: Math.max(0.01, num(E.w[j])),
        size: 0.5 + Math.log1p(num(E.w[j])) * 0.45, hidden: false, hl: false
      });
    }
    var origPos = {};
    for (i = 0; i < n; i++) { origPos[String(i)] = { x: N.x[i], y: N.y[i] }; }

    // ---- colors ----
    var edgeCol = [], edgeColHi = [], edgeColLo = [];
    function computeEdgeColors() {
      types.forEach(function (t, k) {
        var h = safeColor(cfg.edge_colors[t]);
        edgeCol[k] = rgba(h, view.edgeAlpha);
        edgeColHi[k] = rgba(h, Math.min(1, Math.max(0.75, view.edgeAlpha * 2)));
        edgeColLo[k] = rgba(h, Math.min(0.04, view.edgeAlpha * 0.15));
      });
    }
    function baseColor(i) {
      if (view.colorMode === "type") {
        var tc = nodeTypeCount[i], bk = -1, bv = 0;
        for (var k = 0; k < tc.length; k++) { if (tc[k] > bv) { bv = tc[k]; bk = k; } }
        return bk < 0 ? "#8C8C8C" : safeColor(cfg.edge_colors[types[bk]]);
      }
      if (view.colorMode === "community") {
        var ci = communityOf(i);
        return ci >= 0 && ci < COMMUNITY_COLORS.length ? COMMUNITY_COLORS[ci] : "#7a7a7a";
      }
      if (view.colorMode === "topic") {
        var ti = topicNames.indexOf(topicOf(i));
        return ti >= 0 && ti < COMMUNITY_COLORS.length ? COMMUNITY_COLORS[ti] : "#7a7a7a";
      }
      return safeColor(cfg.state_colors[stateName[i]]);
    }
    function computeColors() {
      G.updateEachNodeAttributes(function (key, a) {
        var h = baseColor(+key);
        a.c = rgba(h, 0.92); a.cd = rgba(h, 0.1); a.color = a.c;
        return a;
      }, { attributes: ["c", "cd", "color"] });
    }

    // ---- sizes ----
    function computeSizes() {
      var vals = new Array(n), lo = Infinity, hi = -Infinity;
      for (var i = 0; i < n; i++) {
        var v;
        if (view.sizeMode === "followers") { v = P.followers_count[i]; }
        else if (view.sizeMode === "activity") { v = num(N.pp[i]) + num(N.rp[i]); }
        else { v = N.wd[i]; }
        v = (typeof v === "number" && isFinite(v) && v > 0) ? Math.sqrt(v) : 0;
        vals[i] = v;
        if (i !== ego) { if (v < lo) { lo = v; } if (v > hi) { hi = v; } }  // the owner has its own size
      }
      if (!isFinite(lo)) { lo = 0; hi = 1; }
      var span = hi - lo || 1;
      // Screen-pixel sizes: smaller for large graphs, so that 5,000 nodes do not cover the map.
      var shownCount = view.core && coreSet ? coreSet.size : n;
      var f = Math.max(0.35, Math.min(1, Math.sqrt(400 / Math.max(1, shownCount))));
      G.updateEachNodeAttributes(function (key, a) {
        var i = +key;
        a.size = (i === ego ? Math.max(12, 22 * f) : (3.5 + 15 * (vals[i] - lo) / span) * f + 1.5 * (1 - f)) * view.nodeScale;
        return a;
      }, { attributes: ["size"] });
    }

    // ---- Sigma styles (closures read the view state) ----
    function dimmed(a) { return keep !== null && !keep.has(a.ix); }
    var styles = {
      nodes: {
        x: { attribute: "x" },
        y: { attribute: "y" },
        size: function (a, s) {
          var b = a.size || 3;
          if (s.isHovered) { return b * 1.25 + 2; }
          return (a.hl || a.sel) ? b * 1.2 + 2 : b;
        },
        color: function (a) { return dimmed(a) ? a.cd : a.c; },
        label: { attribute: "label" },
        labelColor: function () { return SigmaView.labelColor; },
        labelSize: 12,
        visibility: function (a) { return a.hidden ? "hidden" : "visible"; },
        labelVisibility: function (a, s) {
          if (a.hidden) { return "hidden"; }
          if (s.isHovered || a.sel || a.hl) { return "visible"; }
          if (keep !== null) { return keep.has(a.ix) && keep.size <= 60 ? "visible" : "hidden"; }
          if (!view.labels) { return "hidden"; }
          return a.ego ? "visible" : "auto";
        },
        backdropVisibility: "hidden",
        depth: function (a, s) { return (s.isHovered || a.sel || a.hl || (keep !== null && keep.has(a.ix))) ? "topNodes" : "nodes"; }
      },
      edges: {
        size: function (a) { return (a.hl ? 2.6 : (a.size || 1)) * view.edgeWidth; },
        color: function (a) {
          if (a.hl) { return "#fff3d6"; }
          if (keep !== null) { return (keep.has(a.si) && keep.has(a.ti) && (hover < 0 || a.si === hover || a.ti === hover)) ? edgeColHi[a.ty] : edgeColLo[a.ty]; }
          return edgeCol[a.ty];
        },
        head: function () { return view.arrows ? "arrow" : "none"; },
        visibility: function (a) { return a.hidden ? "hidden" : "visible"; },
        depth: function (a) { return a.hl || (keep !== null && keep.has(a.si) && keep.has(a.ti)) ? "topEdges" : "edges"; }
      }
    };

    // ---- visibility (filters, time window, playback) ----
    function timeCut() {
      // Returns [from, to] in minutes after t0, or null when no time rule applies.
      if (!hasTimes) { return null; }
      if (pb.open) { return [-Infinity, pb.T]; }
      if (view.window !== null) { return [tMax - view.window, Infinity]; }
      return null;
    }
    function computeVisibility() {
      var shownN = 0, shownE = 0, i, j, k;
      var cut = timeCut();
      for (i = 0; i < n; i++) {
        var on = view.states.has(stateName[i]) && (view.showMissing || !missing[i]) && (!view.core || coreSet.has(i));
        if (on && view.community !== "all") { on = String(communityOf(i)) === view.community; }
        if (on && view.topic !== "all") { on = view.topic === "none" ? topicOf(i) === null : hasTopic(i, view.topic); }
        if (on && view.hubsOnly) { on = HB[i] === 1 || BR[i] === 1; }
        if (on && view.change !== "all") { on = changeOf(i, view.change); }
        if (i === ego) { on = true; }
        nodeShown[i] = on;
      }
      var touched = cut ? new Uint8Array(n) : null;
      var untimedShown = 0;
      for (j = 0; j < m; j++) {
        var typeOk = false;
        for (k = 0; k < types.length; k++) {
          if (view.types.has(types[k]) && (counts[k][j] || 0) > 0) { typeOk = true; break; }
        }
        var on2 = typeOk && nodeShown[E.s[j]] && nodeShown[E.t[j]] && (E.w[j] || 0) >= view.minW && E.s[j] !== E.t[j];
        if (on2 && cut) {
          var f0 = ft[j];
          if (f0 === null || f0 === undefined) {
            // A link with no time shows from the start of playback, and never in a time window.
            on2 = pb.open;
            if (on2) { untimedShown++; }
          } else if (pb.open) {
            on2 = f0 <= cut[1];
          } else {
            on2 = lt[j] >= cut[0];
          }
        }
        edgeShown[j] = on2;
        if (on2) { shownE++; if (touched) { touched[E.s[j]] = 1; touched[E.t[j]] = 1; } }
      }
      for (i = 0; i < n; i++) {
        if (touched && i !== ego && !touched[i]) { nodeShown[i] = false; }
        if (nodeShown[i]) { shownN++; }
      }
      G.updateEachNodeAttributes(function (key, a) { a.hidden = !nodeShown[+key]; return a; }, { attributes: ["hidden"] });
      G.updateEachEdgeAttributes(function (key, a) { a.hidden = !edgeShown[+key.slice(1)]; return a; }, { attributes: ["hidden"] });
      var c = $("vx-counts");
      clear(c);
      c.appendChild(el("strong", shownN.toLocaleString("en-US")));
      c.appendChild(document.createTextNode(" of " + n.toLocaleString("en-US") + " accounts " + MID + " "));
      c.appendChild(el("strong", shownE.toLocaleString("en-US")));
      c.appendChild(document.createTextNode(" of " + m.toLocaleString("en-US") + " links"));
      c.setAttribute("data-shown-nodes", String(shownN));
      c.setAttribute("data-shown-edges", String(shownE));
      if (pb.open) {
        var pc = $("vx-pb-counts");
        clear(pc);
        pc.appendChild(el("strong", shownN.toLocaleString("en-US")));
        pc.appendChild(document.createTextNode(" of " + n.toLocaleString("en-US") + " accounts " + MID + " "));
        pc.appendChild(el("strong", shownE.toLocaleString("en-US")));
        pc.appendChild(document.createTextNode(" links"));
        pc.setAttribute("data-shown-nodes", String(shownN));
        pc.setAttribute("data-shown-edges", String(shownE));
        $("vx-pb-untimed").textContent = untimed ? "Links with no time (shown from the start): " + untimedShown.toLocaleString("en-US") : "";
      }
    }

    function applyFilters() {
      computeVisibility();
      if (pathA >= 0 && pathB >= 0 && pathNodes !== null) { findPath(); }
      if (selected >= 0) { showPanel(selected); }
      SigmaView.refresh();
    }

    // ---- header facts (FR-49), shown in the Overview ----
    function buildFacts() {
      var meta = D.meta, col = meta.collect || {}, w = col.window || {}, facts = $("vx-facts");
      var parts = [];
      if (w.window_start || w.since || w.start_time) {
        parts.push("Collection window: " + (w.window_start || w.since || w.start_time) + " to " + (w.window_end || w.until || w.end_time || "run time"));
      } else if (w.window_days !== undefined) {
        parts.push("Collection window: " + w.window_days + " days before " + (col.finished_at || "the collect run"));
      } else if (col.observed) {
        parts.push("Collection window: not recorded. Interaction times in the data: " + fmt(col.observed.first) + " to " + fmt(col.observed.last));
      } else {
        parts.push("Collection window: not recorded");
      }
      parts.push("Sources: " + ((col.sources && col.sources.length) ? col.sources.join(", ") : "not recorded"));
      parts.push("Nodes: " + n.toLocaleString("en-US") + ", edges: " + m.toLocaleString("en-US"));
      parts.push("Hidden (no profile): " + meta.counts.missing_profile.toLocaleString("en-US"));
      if (meta.ego_handle) { parts.push("Owner: @" + meta.ego_handle); }
      parts.push("Rendered " + meta.generated_at);
      facts.textContent = parts.join(" | ");
      facts.setAttribute("data-hidden-count", String(meta.counts.missing_profile));
      $("vx-limits").textContent =
        "Limits: the data comes only from interactions with the owner's account (ego-only collection). " +
        "There are few links between other accounts, so the map does not show how they connect to each other. " +
        "Signal states describe signals in the data, not persons. A state is not a judgment of an account.";
    }

    // ---- legend ----
    function buildLegend() {
      var lg = $("vx-legend");
      clear(lg);
      var left = [], right = [];
      if (view.colorMode === "community" && community) {
        community.sizes.slice(0, 8).forEach(function (sz, r) {
          left.push([COMMUNITY_COLORS[r] || "#7a7a7a", "Community " + (r + 1), sz, "dot"]);
        });
        left.push(["#7a7a7a", "No group", null, "dot"]);
      } else if (view.colorMode === "topic") {
        topicNames.slice(0, COMMUNITY_COLORS.length).forEach(function (t, r) {
          var c2 = 0;
          for (var q = 0; q < n; q++) { if (topicOf(q) === t) { c2++; } }
          left.push([COMMUNITY_COLORS[r], "Topic: " + t, c2, "dot"]);
        });
        left.push(["#7a7a7a", "No topic", null, "dot"]);
      } else if (view.colorMode === "type") {
        left.push(["#8C8C8C", "Node: main type", null, "none"]);
      } else {
        var sc = {};
        states.forEach(function (s) { sc[s] = 0; });
        for (var i = 0; i < n; i++) { sc[stateName[i]]++; }
        states.slice().reverse().forEach(function (s) {
          left.push([safeColor(cfg.state_colors[s]), cfg.state_marks[s] + " " + cfg.state_labels[s], sc[s], "dot"]);
        });
      }
      types.forEach(function (t) { right.push([safeColor(cfg.edge_colors[t]), TYPE_LABEL[t] || t, null, "dash"]); });
      var rows = Math.max(left.length, right.length);
      for (var r = 0; r < rows; r++) {
        [left[r], right[r]].forEach(function (it) {
          var d = el("div", null, "vx-lg");
          if (it) {
            if (it[3] !== "none") {
              var sw = el("span", null, it[3] === "dash" ? "vx-dash" : "vx-swatch");
              sw.style.backgroundColor = it[0];
              d.appendChild(sw);
            }
            d.appendChild(el("span", it[1]));
            if (it[2] !== null) { d.appendChild(el("span", fmtInt(it[2]), "vx-lg-c")); }
          }
          lg.appendChild(d);
        });
      }
    }

    // ---- top bar: type chips and time window ----
    function buildTopBar() {
      var ft2 = $("vx-f-types");
      types.forEach(function (t, k) {
        var lab = el("label", null, "vx-chip"), cb = el("input");
        cb.type = "checkbox"; cb.checked = true; cb.id = "vx-type-" + t; cb.setAttribute("data-type", t);
        cb.style.setProperty("--chip", safeColor(cfg.edge_colors[t]));
        var nE = 0;
        for (var j = 0; j < m; j++) { if ((counts[k][j] || 0) > 0) { nE++; } }
        lab.title = nE.toLocaleString("en-US") + " links with a " + (TYPE_ONE[t] || t);
        cb.addEventListener("change", function () {
          if (cb.checked) { view.types.add(t); } else { view.types.delete(t); }
          lab.classList.toggle("vx-off", !cb.checked);
          applyFilters();
        });
        lab.appendChild(cb);
        lab.appendChild(document.createTextNode(TYPE_LABEL[t] || t));
        ft2.appendChild(lab);
      });
      var wins = $("vx-windows").querySelectorAll("button[data-window]");
      Array.prototype.forEach.call(wins, function (b) {
        if (!hasTimes && b.getAttribute("data-window") !== "all") { b.disabled = true; b.title = "The data has no interaction times."; }
        b.addEventListener("click", function () {
          var v = b.getAttribute("data-window");
          view.window = v === "all" ? null : parseInt(v, 10);
          Array.prototype.forEach.call(wins, function (x) { x.setAttribute("aria-pressed", String(x === b)); });
          applyFilters();
        });
      });
    }

    // ---- controls drawer ----
    function bindRange(id, fmtFn, fn) {
      var r = $(id), v = $(id + "-v");
      function upd() { if (v) { v.textContent = fmtFn(parseFloat(r.value)); } }
      upd();
      r.addEventListener("input", function () { upd(); fn(parseFloat(r.value)); });
    }
    function buildControls() {
      var sCount = {};
      states.forEach(function (s) { sCount[s] = 0; });
      for (var i = 0; i < n; i++) { sCount[stateName[i]]++; }
      var fs = $("vx-f-states");
      states.forEach(function (s) {
        var lab = el("label"), cb = el("input");
        cb.type = "checkbox"; cb.checked = true; cb.setAttribute("data-state", s); cb.id = "vx-state-" + s;
        cb.addEventListener("change", function () { if (cb.checked) { view.states.add(s); } else { view.states.delete(s); } applyFilters(); });
        var sw = el("span", null, "vx-swatch"); sw.style.backgroundColor = safeColor(cfg.state_colors[s]);
        lab.appendChild(cb); lab.appendChild(sw);
        lab.appendChild(el("span", cfg.state_marks[s], "vx-mark"));
        lab.appendChild(document.createTextNode(cfg.state_labels[s] + " (" + sCount[s] + ")"));
        fs.appendChild(lab);
      });
      var minw = $("vx-minw");
      minw.value = String(view.minW);
      minw.addEventListener("input", function () {
        var v = parseFloat(minw.value);
        view.minW = isFinite(v) && v > 0 ? v : 0;
        applyFilters();
      });
      var size = $("vx-size");
      size.value = view.sizeMode;
      size.addEventListener("change", function () { view.sizeMode = size.value; computeSizes(); SigmaView.refresh(); });
      var core = $("vx-core");
      if (coreSet === null) {
        core.checked = false; core.disabled = true;
        $("vx-core-note").textContent = "The graph has " + n + " nodes. The core view starts above " + coreMax + " nodes.";
      } else {
        core.checked = true;
        $("vx-core-note").textContent = "Shows the owner and the top " + (coreSet.size - (ego >= 0 ? 1 : 0)) +
          " accounts by weighted degree, of " + n + " nodes. Clear it to show all.";
      }
      core.addEventListener("change", function () { view.core = core.checked && coreSet !== null; computeSizes(); applyFilters(); });
      var miss = $("vx-show-missing");
      miss.checked = false;
      miss.addEventListener("change", function () { view.showMissing = miss.checked; applyFilters(); });
      var fc = $("vx-f-community");
      function opt(sel, value, text) { var o = el("option", text); o.value = value; sel.appendChild(o); }
      opt(fc, "all", "All communities");
      if (community) {
        community.sizes.forEach(function (sz, r) { opt(fc, String(r), "Community " + (r + 1) + " (" + sz + ")"); });
      }
      opt(fc, "-1", "No group");
      fc.addEventListener("change", function () { view.community = fc.value; applyFilters(); });
      var ftp = $("vx-f-topic");
      opt(ftp, "all", "All topics");
      topicNames.forEach(function (t) {
        var c3 = 0;
        for (var q = 0; q < n; q++) { if (hasTopic(q, t)) { c3++; } }
        opt(ftp, t, t + " (" + c3 + ")");
      });
      opt(ftp, "none", "No topic");
      ftp.addEventListener("change", function () { view.topic = ftp.value; applyFilters(); });
      var hubs = $("vx-f-hubs");
      var nh = 0, nb = 0;
      for (var q2 = 0; q2 < n; q2++) { if (HB[q2] === 1) { nh++; } if (BR[q2] === 1) { nb++; } }
      $("vx-f-hubs-note").textContent = nh + " hubs and " + nb + " bridges. Hubs have the most links to other accounts. " +
        "Bridges have high betweenness or links into two or more other communities (owner removed).";
      hubs.addEventListener("change", function () { view.hubsOnly = hubs.checked; applyFilters(); });
      var fch = $("vx-f-change");
      opt(fch, "all", "All accounts");
      if (CH) {
        [["any", "Any change"], ["new", "New accounts"], ["state", "State changed"], ["up", "More signals"],
         ["rules", "New rules fired"], ["moved", "Moved to another community"], ["tie", "Stronger tie to you"]].forEach(function (c) {
          var k = 0;
          for (var q = 0; q < n; q++) { if (changeOf(q, c[0])) { k++; } }
          opt(fch, c[0], c[1] + " (" + k + ")");
        });
        $("vx-f-change-note").textContent = "Compared with the snapshot of " + shortDate(CH.base_at) + ". " +
          fmtInt(CH.counts.gone_accounts) + " accounts of that snapshot are not in this map.";
      } else {
        fch.disabled = true;
        $("vx-f-change-note").textContent = "No earlier snapshot. Each render keeps one, so the next run shows the changes.";
      }
      fch.addEventListener("change", function () { view.change = fch.value; applyFilters(); });
      if (community) {
        $("vx-comm-note").textContent = "Leiden, " + fmt(groupsMeta.leiden_runs) + " random seeds, modularity " +
          fmt(groupsMeta.modularity) + ", mean stability " + fmt(groupsMeta.mean_stability) +
          ". The groups are exploratory: an algorithm made them, not the accounts.";
      }
      $("vx-labels").addEventListener("change", function (ev) { view.labels = ev.target.checked; SigmaView.refresh(); });
      $("vx-arrows").addEventListener("change", function (ev) { view.arrows = ev.target.checked; SigmaView.refresh(); });
      $("vx-color").addEventListener("change", function (ev) { setColorMode(ev.target.value); });
      bindRange("vx-node-scale", function (v) { return v.toFixed(1) + "x"; }, function (v) { view.nodeScale = v; computeSizes(); SigmaView.refresh(); });
      bindRange("vx-label-density", function (v) { return v.toFixed(1); }, function (v) {
        SigmaView.setSetting("labelDensity", v);
        SigmaView.setSetting("labelRenderedSizeThreshold", Math.max(0.5, 6.5 - v * 2.5));
      });
      bindRange("vx-edge-opacity", function (v) { return Math.round(v * 100) + "%"; }, function (v) { view.edgeAlpha = v; computeEdgeColors(); SigmaView.refresh(); });
      bindRange("vx-edge-width", function (v) { return v.toFixed(1) + "x"; }, function (v) { view.edgeWidth = v; SigmaView.refresh(); });
      bindRange("vx-fa2-gravity", function (v) { return v.toFixed(2); }, function () { fa2.settingsDirty = true; });
      bindRange("vx-fa2-scaling", function (v) { return v.toFixed(1); }, function () { fa2.settingsDirty = true; });
      bindRange("vx-fa2-ewi", function (v) { return v.toFixed(1); }, function () { fa2.settingsDirty = true; });
      $("vx-fa2-run").addEventListener("click", function () { if (fa2.running) { fa2Stop(); } else { fa2Start(); } });
      $("vx-layout-reset").addEventListener("click", function () { $("vx-layout").value = "ego"; applyLayout("ego"); });
      $("vx-layout").addEventListener("change", function (ev) { applyLayout(ev.target.value); });
    }

    function setColorMode(mode) {
      // Communities come from Leiden in the analyze stage, with the owner removed (AD-07,
      // FR-26): the owner links to every account, so with the owner in the graph one group
      // holds almost all accounts. Accounts that link only to the owner have no group.
      if (mode === "community" && !community) { $("vx-color").value = view.colorMode; return; }
      if (mode === "community") { $("vx-graph").setAttribute("data-communities", community.sizes.join(",")); }
      if (mode === "topic" && !topicNames.length) { $("vx-color").value = view.colorMode; return; }
      view.colorMode = mode;
      computeColors();
      buildLegend();
      SigmaView.refresh();
    }

    // ---- layout: presets, live ForceAtlas2, drag ----
    var fa2 = { running: false, iters: 0, perIter: 0, batch: 1, settingsDirty: true, settings: null, limit: 2000 };
    function fa2Settings() {
      if (fa2.settingsDirty || !fa2.settings) {
        var base = window.graphologyLibrary.layoutForceAtlas2.inferSettings(G);
        base.gravity = parseFloat($("vx-fa2-gravity").value);
        base.scalingRatio = parseFloat($("vx-fa2-scaling").value);
        base.edgeWeightInfluence = parseFloat($("vx-fa2-ewi").value);
        base.linLogMode = $("vx-fa2-linlog").checked;
        base.adjustSizes = $("vx-fa2-overlap").checked;
        base.barnesHutOptimize = n > 800;
        fa2.settings = base;
        fa2.settingsDirty = false;
      }
      return fa2.settings;
    }
    ["vx-fa2-linlog", "vx-fa2-overlap"].forEach(function (id) { $(id).addEventListener("change", function () { fa2.settingsDirty = true; }); });
    function layoutStatus(text) { $("vx-layout-status").textContent = text; }
    function fa2Frame() {
      if (!fa2.running) { return; }
      var t = performance.now();
      window.graphologyLibrary.layoutForceAtlas2.assign(G, { iterations: fa2.batch, settings: fa2Settings(), getEdgeWeight: "weight" });
      var dt = performance.now() - t;
      fa2.iters += fa2.batch;
      fa2.perIter = dt / fa2.batch;
      fa2.batch = Math.max(1, Math.min(40, Math.floor(14 / Math.max(0.05, fa2.perIter))));
      $("vx-graph").setAttribute("data-layout-iterations", String(fa2.iters));
      layoutStatus("Force layout running: " + fa2.iters.toLocaleString("en-US") + " steps");
      if (fa2.iters >= fa2.limit) { fa2Stop(); layoutStatus("Force layout stopped after " + fa2.iters.toLocaleString("en-US") + " steps. Start it again to continue."); return; }
      requestAnimationFrame(fa2Frame);
    }
    function fa2Start() {
      if (fa2.running) { return; }
      fa2.running = true; fa2.limit = fa2.iters + 2000; fa2.settingsDirty = true;
      $("vx-fa2-run").textContent = "Stop force";
      $("vx-fa2-run").setAttribute("aria-pressed", "true");
      $("vx-layout").value = "force";
      requestAnimationFrame(fa2Frame);
    }
    function fa2Stop() {
      fa2.running = false;
      $("vx-fa2-run").textContent = "Start force";
      $("vx-fa2-run").setAttribute("aria-pressed", "false");
      layoutStatus("");
    }
    function stateLayout() {
      // One disc per state, larger nodes in the middle of each disc.
      var groups = {};
      states.forEach(function (s) { groups[s] = []; });
      for (var i = 0; i < n; i++) { if (i !== ego) { groups[stateName[i]].push(i); } }
      var order2 = ["no_signals", "some_signals", "many_signals", "not_scored"].filter(function (s) { return groups[s] && groups[s].length; });
      var radii = order2.map(function (s) { return Math.sqrt(groups[s].length) * 6 + 10; });
      var total = radii.reduce(function (a, b) { return a + b; }, 0) * 2 + 40 * order2.length;
      var targets = {}, x = -total / 2;
      order2.forEach(function (s, gi) {
        var r = radii[gi];
        x += r + 20;
        var g = groups[s].slice().sort(function (a, b) { return num(N.wd[b]) - num(N.wd[a]); });
        g.forEach(function (node, q) {
          var rr = 6 * Math.sqrt(q + 0.5), a = q * 2.39996323;
          targets[String(node)] = { x: x + rr * Math.cos(a), y: rr * Math.sin(a) };
        });
        x += r + 20;
      });
      if (ego >= 0) { targets[String(ego)] = { x: 0, y: -Math.max.apply(null, radii.concat([10])) - 40 }; }
      return targets;
    }
    function applyLayout(kind) {
      fa2Stop();
      G.updateEachNodeAttributes(function (k, a) { a.fixed = false; return a; }, { attributes: ["fixed"] });
      if (kind === "force") { fa2Start(); return; }
      var targets = kind === "state" ? stateLayout() : origPos;
      SigmaView.animateTo(targets, function () { SigmaView.fit(); });
      $("vx-graph").setAttribute("data-layout", kind);
    }

    // ---- search (FR-45) ----
    function buildSearch() {
      var input = $("vx-search"), list = $("vx-results");
      var lowH = N.h.map(function (h) { return h ? String(h).toLocaleLowerCase() : ""; });
      var lowN = N.nm.map(function (x) { return x ? String(x).toLocaleLowerCase() : ""; });
      function close() { list.classList.remove("vx-open"); }
      function run() {
        clear(list);
        var q = input.value.trim().toLocaleLowerCase();
        if (q.charAt(0) === "@") { q = q.slice(1); }
        if (!q) { close(); return; }
        var hits = [];
        for (var i = 0; i < n && hits.length < 50; i++) {
          if (lowH[i].indexOf(q) >= 0 || lowN[i].indexOf(q) >= 0) { hits.push(i); }
        }
        hits.sort(function (a, b) { return (lowH[a].indexOf(q) === 0 ? 0 : 1) - (lowH[b].indexOf(q) === 0 ? 0 : 1) || num(N.wd[b]) - num(N.wd[a]); });
        list.classList.add("vx-open");
        if (!hits.length) { list.appendChild(el("li", "No match.", "vx-muted")); return; }
        hits.forEach(function (i) {
          var li = el("li"), b = el("button");
          b.type = "button";
          b.setAttribute("data-node", String(i));
          var sw = el("span", null, "vx-swatch"); sw.style.backgroundColor = safeColor(cfg.state_colors[stateName[i]]);
          b.appendChild(sw);
          b.appendChild(el("span", markOf(i), "vx-mark"));
          b.appendChild(el("span", nameOf(i), "vx-r-h vx-isolate"));
          if (N.nm[i] && N.h[i]) { b.appendChild(el("span", N.nm[i], "vx-r-n vx-isolate")); }
          if (!nodeShown[i]) { b.appendChild(el("span", "(hidden by a filter)", "vx-r-x")); }
          b.addEventListener("click", function () { close(); goToNode(i); });
          li.appendChild(b); list.appendChild(li);
        });
      }
      input.addEventListener("input", function () {
        input.parentNode.classList.toggle("vx-has-text", input.value.length > 0);
        run();
      });
      input.addEventListener("focus", function () { if (input.value.trim()) { run(); } });
      input.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") { var b = list.querySelector("button[data-node]"); if (b) { b.click(); } }
        if (ev.key === "Escape") { close(); input.blur(); }
      });
      document.addEventListener("mousedown", function (ev) { if (!ev.target.closest || !ev.target.closest(".vx-searchbox")) { close(); } });
    }

    // ---- drawers and views ----
    function setLeft(open) {
      $("vx-left").hidden = !open;
      $("vx-btn-findings").setAttribute("aria-pressed", String(open));
      $("vx-mapview").classList.toggle("vx-left-open", open);
    }
    function setRight(open, tab) {
      $("vx-right").hidden = !open;
      $("vx-mapview").classList.toggle("vx-right-open", open);
      if (tab) {
        var acc = tab === "account";
        $("vx-tab-account").setAttribute("aria-selected", String(acc));
        $("vx-tab-controls").setAttribute("aria-selected", String(!acc));
        $("vx-panel").hidden = !acc;
        $("vx-controls").hidden = acc;
      }
      $("vx-btn-controls").setAttribute("aria-pressed", String(open && $("vx-panel").hidden));
    }
    function setView(v) {
      var map = v === "map";
      $("vx-mapview").hidden = !map;
      $("vx-overview").hidden = map;
      $("vx-view-map").setAttribute("aria-pressed", String(map));
      $("vx-view-overview").setAttribute("aria-pressed", String(!map));
      document.body.setAttribute("data-view", v);
      if (map && SigmaView.r) { drawBackground(); SigmaView.resize(); }
    }
    function goToNode(i) {
      setView("map");
      select(i, true);
    }

    // ---- account panel (FR-44, SR-02, SR-07) ----
    function addRow(dl, k, v) { dl.appendChild(el("dt", k)); dl.appendChild(el("dd", fmt(v))); }
    function urlNode(u) {
      var ok = false;
      try { var parsed = new URL(String(u)); ok = parsed.protocol === "http:" || parsed.protocol === "https:"; } catch (e) { ok = false; }
      if (ok) {
        var a = el("a", u);
        a.href = String(u);
        a.rel = "noopener noreferrer nofollow";
        a.target = "_blank";
        return a;
      }
      var s = el("span", u, "vx-isolate");
      s.setAttribute("data-not-link", "1");
      var wrap = el("span");
      wrap.appendChild(s);
      wrap.appendChild(el("span", " (not a link: the scheme is not http or https)", "vx-muted"));
      return wrap;
    }
    function stat(box, k, v) {
      var d = el("div", null, "vx-stat");
      d.appendChild(el("div", v, "vx-stat-v"));
      d.appendChild(el("div", k, "vx-stat-k"));
      box.appendChild(d);
    }
    function statePill(i) {
      var st = stateName[i];
      var sline = el("div", null, "vx-statepill"); sline.id = "vx-p-state";
      var sw = el("span", null, "vx-swatch"); sw.style.backgroundColor = safeColor(cfg.state_colors[st]);
      sline.appendChild(sw);
      sline.appendChild(el("span", cfg.state_marks[st], "vx-mark"));
      sline.appendChild(document.createTextNode(cfg.state_labels[st]));
      return sline;
    }
    function showPanel(i) {
      var pane = $("vx-panel");
      clear(pane);
      var h2 = el("h2", null, "vx-isolate");
      h2.id = "vx-p-name";
      h2.textContent = N.nm[i] ? N.nm[i] : nameOf(i);
      pane.appendChild(h2);
      var hd = el("div", N.h[i] ? "@" + N.h[i] : "Handle not in data", "vx-isolate");
      hd.id = "vx-p-handle";
      pane.appendChild(hd);
      pane.appendChild(statePill(i));
      if (i === ego) { pane.appendChild(el("div", "This is the owner's account.", "vx-note")); }
      if (!D.hs[i]) { pane.appendChild(el("div", "No state record from the score stage.", "vx-note")); }
      if (!nodeShown[i]) { pane.appendChild(el("div", "A filter hides this account in the map now.", "vx-note vx-warn")); }
      if (missing[i]) { pane.appendChild(el("div", "No profile record for this account (profile_status = missing).", "vx-note")); }

      var stats = el("div", null, "vx-stats");
      stat(stats, "Followers", compact(P.followers_count[i]));
      stat(stats, "Following", compact(P.following_count[i]));
      stat(stats, "Posts", compact(P.tweet_count[i]));
      var age = ageYears(i);
      stat(stats, "Account age", age === null ? "-" : (age < 1 ? Math.round(age * 12) + " mo" : (Math.round(age * 10) / 10) + " y"));
      stat(stats, "Tie weight", compact(N.ew[i]));
      stat(stats, "Links in / out", fmtInt(N.din[i]) + " / " + fmtInt(N.dout[i]));
      pane.appendChild(stats);

      var bio = el("div", P.description[i] === null ? "Bio not in data" : P.description[i], "vx-bio");
      bio.id = "vx-p-bio";
      pane.appendChild(bio);

      var dl = el("dl");
      dl.appendChild(el("dt", "URL"));
      var dd = el("dd"); dd.id = "vx-p-url";
      if (P.url[i]) { dd.appendChild(urlNode(P.url[i])); } else { dd.textContent = "not in data"; }
      dl.appendChild(dd);
      addRow(dl, "Location", P.location[i]);
      addRow(dl, "Created", P.created_at[i]);
      addRow(dl, "Listed", P.listed_count[i]);
      addRow(dl, "Verified type", P.verified_type[i]);
      addRow(dl, "Protected", P.protected[i] === null ? null : (P.protected[i] ? "yes" : "no"));
      addRow(dl, "User ID", N.id[i]);
      pane.appendChild(dl);

      if (CH && i !== ego && changeOf(i, "any")) {
        pane.appendChild(el("h3", "Since " + shortDate(CH.base_at), "vx-sub"));
        var cl = el("dl"); cl.id = "vx-p-changes";
        if (CN[i] === 1) { addRow(cl, "Account", "new in this map"); }
        if (changeOf(i, "state")) {
          addRow(cl, "State", cfg.state_labels[states[CPS[i]]] + " -> " + cfg.state_labels[stateName[i]]);
        }
        if (CRA[i] && CRA[i].length) { addRow(cl, "New rules", CRA[i].join(", ")); }
        if (CMV[i] === 1) { addRow(cl, "Community", "moved to another community"); }
        if (typeof CTW[i] === "number") { addRow(cl, "Tie weight to you", (CTW[i] > 0 ? "+" : "") + fmt(CTW[i])); }
        pane.appendChild(cl);
      }

      if (i !== ego && CS[i] !== null && CS[i] !== undefined) {
        pane.appendChild(el("h3", "Groups (exploratory)", "vx-sub"));
        var gl = el("dl"); gl.id = "vx-p-groups";
        var ci2 = communityOf(i);
        addRow(gl, "Community", ci2 >= 0 ? "Community " + (ci2 + 1) + " of " + community.sizes.length : "No group");
        addRow(gl, "Stability", Math.round(num(CS[i]) * 100) + " % of " + fmt(groupsMeta.leiden_runs) + " Leiden runs agree");
        var roles = [];
        if (HB[i] === 1) { roles.push("hub"); }
        if (BR[i] === 1) { roles.push("bridge (links into " + fmt(BC[i]) + " other communities)"); }
        addRow(gl, "Role", roles.length ? roles.join(", ") : "none");
        var tl = (TC[i] || []).slice();
        if (TI[i]) { tl.push(TI[i] + " (from links)"); }
        addRow(gl, "Topics", tl.length ? tl.join(", ") : "none");
        pane.appendChild(gl);
      }

      pane.appendChild(el("h3", "Signals", "vx-sub"));
      var rs = rulesOf[i] || [];
      if (!rs.length) {
        pane.appendChild(el("div", "No rule fired.", "vx-note"));
      } else {
        var ul = el("ul", null, "vx-rules"); ul.id = "vx-p-rules";
        rs.forEach(function (r) {
          var li = el("li");
          li.appendChild(el("span", r[1], "vx-rule-id"));
          li.appendChild(el("span", " " + r[2] + ", " + (r[3] >= 0 ? "+" : "") + r[3] + " points" + (r[5] ? ", hard rule" : ""), "vx-rule-meta"));
          li.appendChild(el("div", r[4], "vx-isolate"));
          ul.appendChild(li);
        });
        pane.appendChild(ul);
      }
      if (D.n.tp[i] !== null && D.n.tp[i] !== undefined) { pane.appendChild(el("div", "Total points: " + fmt(D.n.tp[i]), "vx-note")); }
      var ov = D.ov[String(i)];
      if (ov) {
        var od = el("div", null, "vx-note vx-warn"); od.id = "vx-p-override";
        od.appendChild(document.createTextNode("Owner override note: "));
        od.appendChild(el("span", ov, "vx-isolate"));
        pane.appendChild(od);
      }

      var fl = findingsOf[i] || [];
      if (fl.length) {
        pane.appendChild(el("h3", "Findings", "vx-sub"));
        var ul2 = el("ul", null, "vx-list"); ul2.id = "vx-p-findings";
        fl.forEach(function (fi) {
          var li = el("li"), b = el("button");
          b.type = "button";
          var sev = el("span", F[fi].sev, "vx-sev vx-sev-" + F[fi].sev);
          b.appendChild(sev);
          b.appendChild(el("span", F[fi].title, "vx-l-h vx-isolate"));
          b.addEventListener("click", function () { focusOnFinding(fi); });
          li.appendChild(b); ul2.appendChild(li);
        });
        pane.appendChild(ul2);
      }

      pane.appendChild(el("h3", "Activity in this data", "vx-sub"));
      var dl3 = el("dl");
      addRow(dl3, "Posts seen", N.pp ? N.pp[i] : null);
      addRow(dl3, "Reposts seen", N.rp ? N.rp[i] : null);
      addRow(dl3, "First link", timeText(nodeFirst[i], true));
      addRow(dl3, "Last link", timeText(nodeLast[i], true));
      pane.appendChild(dl3);

      pane.appendChild(el("h3", "Graph metrics", "vx-sub"));
      var dl2 = el("dl");
      addRow(dl2, "Degree", N.dg[i]);
      addRow(dl2, "In / out", fmt(N.din[i]) + " / " + fmt(N.dout[i]));
      addRow(dl2, "Weighted degree", N.wd[i]);
      addRow(dl2, "Tie weight to owner", N.ew[i]);
      addRow(dl2, "PageRank", N.pr[i]);
      addRow(dl2, "Betweenness (owner removed)", N.bt[i]);
      pane.appendChild(dl2);

      var bar = el("div", null, "vx-actions");
      [["Path from here", function () { setPathEnd("a", i); }, "vx-p-from"],
       ["Path to here", function () { setPathEnd("b", i); }, "vx-p-to"],
       ["Center", function () { SigmaView.focusNode(String(i)); }, "vx-p-center"]].forEach(function (x) {
        var b = el("button", x[0], "vx-btn"); b.type = "button"; b.id = x[2]; b.addEventListener("click", x[1]); bar.appendChild(b);
      });
      pane.appendChild(bar);
    }

    function select(i, focus) {
      if (selected >= 0) { G.setNodeAttribute(String(selected), "sel", false); }
      selected = i;
      G.setNodeAttribute(String(i), "sel", true);
      showPanel(i);
      setRight(true, "account");
      $("vx-right").querySelector(".vx-drawer-body").scrollTop = 0;
      if (focus && nodeShown[i]) { SigmaView.focusNode(String(i)); }
    }
    function deselect() {
      if (selected >= 0) { G.setNodeAttribute(String(selected), "sel", false); }
      selected = -1;
    }

    // ---- hover card ----
    var mouse = { x: 0, y: 0 };
    function showHover(i) {
      var box = $("vx-hover");
      clear(box);
      var l1 = el("div");
      l1.appendChild(el("span", nameOf(i), "vx-hv-h vx-isolate"));
      l1.appendChild(document.createTextNode(" " + MID + " " + cfg.state_labels[stateName[i]]));
      box.appendChild(l1);
      box.appendChild(el("div", fmtInt(N.pp ? N.pp[i] : 0) + " posts, " + fmtInt(N.rp ? N.rp[i] : 0) + " reposts seen"));
      box.appendChild(el("div", fmtInt(N.din[i]) + " incoming " + MID + " " + fmtInt(N.dout[i]) + " outgoing links"));
      box.hidden = false;
      placeHover();
    }
    function placeHover() {
      var box = $("vx-hover");
      if (box.hidden) { return; }
      var st = $("vx-stage"), w = st.clientWidth, h = st.clientHeight;
      var x = mouse.x + 16, y = mouse.y + 16;
      if (x + box.offsetWidth > w - 8) { x = mouse.x - box.offsetWidth - 12; }
      if (y + box.offsetHeight > h - 8) { y = mouse.y - box.offsetHeight - 12; }
      box.style.left = Math.max(4, x) + "px";
      box.style.top = Math.max(4, y) + "px";
    }
    // Hover restyles every node and link (the rest dim), which costs a full refresh.
    // During a pan the cursor crosses many nodes, so the highlight waits until the
    // mouse button is up and the pointer rests (frame time at 5k nodes: 29 ms -> 18 ms).
    var hoverTimer = null, pointerDown = false, HOVER_DELAY_MS = 90;
    function setHover(i) {
      if (hoverTimer !== null) { clearTimeout(hoverTimer); hoverTimer = null; }
      if (i >= 0 && pointerDown) { return; }
      if (i < 0 && hover < 0) { return; }
      if (i < 0) { applyHover(-1); return; }
      hoverTimer = setTimeout(function () { hoverTimer = null; applyHover(i); }, HOVER_DELAY_MS);
    }
    function applyHover(i) {
      hover = i;
      if (i >= 0) {
        var s = new Set([i]);
        G.forEachNeighbor(String(i), function (k) { s.add(+k); });
        keep = s;
        showHover(i);
      } else {
        keep = focusSet;
        $("vx-hover").hidden = true;
      }
      SigmaView.refreshStyles();
    }

    // ---- findings (left drawer) ----
    function setFocus(members, text, fi) {
      focusSet = members && members.length ? new Set(members) : null;
      focusFinding = fi === undefined ? -1 : fi;
      keep = focusSet;
      G.updateEachNodeAttributes(function (k, a) { a.hl = focusSet !== null && focusSet.has(+k); return a; }, { attributes: ["hl"] });
      var box = $("vx-focus");
      box.hidden = focusSet === null;
      $("vx-focus-text").textContent = text || "";
      var g = $("vx-graph");
      if (focusSet) { g.setAttribute("data-focus", members.map(function (x) { return N.id[x]; }).join(",")); }
      else { g.removeAttribute("data-focus"); }
      Array.prototype.forEach.call(document.querySelectorAll(".vx-finding"), function (c) {
        c.classList.toggle("vx-active", +c.getAttribute("data-finding") === focusFinding);
      });
      SigmaView.refresh();
      if (focusSet) { SigmaView.fitNodes(members.filter(function (x) { return nodeShown[x]; }).map(String)); }
    }
    function focusOnFinding(fi) {
      var f = F[fi];
      setView("map");
      var hiddenCount = f.m.filter(function (x) { return !nodeShown[x]; }).length;
      setFocus(f.m.slice(), "Finding: " + f.title + (hiddenCount ? " (" + hiddenCount + " hidden by a filter)" : ""), fi);
    }
    function buildFindings() {
      var box = $("vx-findings");
      clear(box);
      if (!F.length) {
        box.appendChild(el("div", "No findings in this render. The score stage found no pattern across accounts, or it did not run the pattern detectors.", "vx-empty"));
      }
      F.forEach(function (f, fi) {
        var card = el("div", null, "vx-finding");
        card.setAttribute("data-finding", String(fi));
        card.tabIndex = 0;
        card.setAttribute("role", "button");
        var top = el("div", null, "vx-finding-top");
        top.appendChild(el("span", f.sev, "vx-sev vx-sev-" + f.sev));
        if (f["new"] === 1) { top.appendChild(el("span", "new", "vx-new")); card.setAttribute("data-new", "1"); }
        top.appendChild(el("span", f.title, "vx-finding-title vx-isolate"));
        card.appendChild(top);
        card.appendChild(el("div", f.sum, "vx-finding-sum vx-isolate"));
        var mem = el("div", null, "vx-members");
        f.m.slice(0, 24).forEach(function (x) {
          var b = el("button", nameOf(x), "vx-member vx-isolate");
          b.type = "button";
          b.setAttribute("data-node", String(x));
          b.addEventListener("click", function (ev) { ev.stopPropagation(); goToNode(x); });
          mem.appendChild(b);
        });
        if (f.m.length > 24) { mem.appendChild(el("span", "+" + (f.m.length - 24) + " more", "vx-muted")); }
        if (f.mx && f.mx.length) { mem.appendChild(el("span", f.mx.length + " not in the map", "vx-muted")); }
        card.appendChild(mem);
        var ev = f.ev;
        if (ev && typeof ev === "object" && Object.keys(ev).length) {
          var det = el("details", null, "vx-evidence");
          det.appendChild(el("summary", "Evidence (" + (f.det || "detector") + ")"));
          var dl = el("dl");
          Object.keys(ev).slice(0, 30).forEach(function (key) {
            var v = ev[key];
            dl.appendChild(el("dt", key));
            dl.appendChild(el("dd", typeof v === "string" ? v : JSON.stringify(v), "vx-isolate"));
          });
          det.appendChild(dl);
          det.addEventListener("click", function (e) { e.stopPropagation(); });
          card.appendChild(det);
        }
        card.addEventListener("click", function () { focusOnFinding(fi); });
        card.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); focusOnFinding(fi); } });
        box.appendChild(card);
      });
      var fl = $("vx-flagged");
      clear(fl);
      var flagged = [];
      for (var i = 0; i < n; i++) { if (stateName[i] === "many_signals" || stateName[i] === "some_signals") { flagged.push(i); } }
      flagged.sort(function (a, b) { return N.st[b] - N.st[a] || num(N.tp[b]) - num(N.tp[a]) || a - b; });
      if (!flagged.length) { fl.appendChild(el("li", "No account has some or many signals.", "vx-empty")); }
      flagged.slice(0, 300).forEach(function (i) {
        var li = el("li"), b = el("button");
        b.type = "button";
        b.setAttribute("data-node", String(i));
        var sw = el("span", null, "vx-swatch"); sw.style.backgroundColor = safeColor(cfg.state_colors[stateName[i]]);
        b.appendChild(sw);
        b.appendChild(el("span", markOf(i), "vx-mark"));
        b.appendChild(el("span", nameOf(i), "vx-l-h vx-isolate"));
        b.appendChild(el("span", cfg.state_labels[stateName[i]] + (N.tp[i] ? " " + MID + " " + fmt(N.tp[i]) + " pts" : ""), "vx-l-x"));
        b.addEventListener("click", function () { goToNode(i); });
        li.appendChild(b); fl.appendChild(li);
      });
      if (flagged.length > 300) { fl.appendChild(el("li", (flagged.length - 300) + " more. Use the Overview table.", "vx-empty")); }
    }

    // ---- playback ----
    function pbRender() {
      var r = $("vx-pb-range");
      r.value = String(Math.round(pb.T));
      $("vx-pb-now").textContent = timeText(pb.T, true);
      var d = new Date((t0 + pb.T * 60) * 1000);
      $("vx-clock-time").textContent = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
      $("vx-clock-date").textContent = d.toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short", year: "numeric" });
      computeVisibility();
      SigmaView.refresh();
    }
    function pbSetPlaying(on) {
      pb.playing = on;
      var b = $("vx-pb-play");
      b.textContent = on ? "Pause" : "Play";
      b.setAttribute("aria-pressed", String(on));
      if (on) {
        if (pb.T >= tMax) { pb.T = 0; }
        pb.last = performance.now();
        pb.lastDraw = 0;
        requestAnimationFrame(pbTick);
      }
    }
    function pbTick(now) {
      if (!pb.playing || !pb.open) { return; }
      var dt = Math.max(0, now - pb.last);
      pb.last = now;
      pb.T = Math.min(tMax, pb.T + tMax * dt / (pb.speed * 1000));
      if (now - (pb.lastDraw || 0) > 90 || pb.T >= tMax) { pb.lastDraw = now; pbRender(); }
      if (pb.T >= tMax) { pbSetPlaying(false); return; }
      requestAnimationFrame(pbTick);
    }
    function setPlayback(open) {
      pb.open = open && hasTimes;
      $("vx-playbar").hidden = !open;
      $("vx-clock").hidden = !pb.open;
      $("vx-btn-playback").setAttribute("aria-pressed", String(open));
      if (!pb.open) {
        pbSetPlaying(false);
        computeVisibility();
        SigmaView.refresh();
        if (open && !hasTimes) { $("vx-pb-now").textContent = "The data has no interaction times. Playback is not possible."; }
        return;
      }
      pb.T = tMax;
      pbRender();
      SigmaView.fit();
    }
    function buildPlayback() {
      var r = $("vx-pb-range");
      r.max = String(Math.max(1, tMax));
      r.value = String(tMax);
      if (!hasTimes) { r.disabled = true; $("vx-pb-play").disabled = true; $("vx-pb-rewind").disabled = true; }
      r.addEventListener("input", function () { pb.T = Math.min(tMax, Math.max(0, parseFloat(r.value) || 0)); pbSetPlaying(false); pbRender(); });
      $("vx-pb-play").addEventListener("click", function () { pbSetPlaying(!pb.playing); });
      $("vx-pb-rewind").addEventListener("click", function () { pbSetPlaying(false); pb.T = 0; pbRender(); });
      $("vx-pb-speed").addEventListener("change", function (ev) { pb.speed = parseInt(ev.target.value, 10) || 60; });
    }

    // ---- overview ----
    function kv(parent, label, value, color, kind) {
      var row = el("div", null, "vx-kv");
      var k = el("span", null, "vx-k");
      if (color) {
        var sw = el("span", null, kind === "dash" ? "vx-dash" : "vx-swatch");
        sw.style.backgroundColor = color;
        k.appendChild(sw);
      }
      k.appendChild(el("span", label, "vx-isolate"));
      row.appendChild(k);
      row.appendChild(el("span", value, "vx-v"));
      parent.appendChild(row);
      return row;
    }
    function bar(parent, frac, color) {
      var b = el("div", null, "vx-bar"), s = el("span");
      s.style.width = Math.max(0, Math.min(100, frac * 100)).toFixed(1) + "%";
      if (color) { s.style.backgroundColor = color; }
      b.appendChild(s); parent.appendChild(b);
    }
    function buildOverview() {
      // Accounts by state, with a stacked bar.
      var c1 = $("vx-ov-states"); clear(c1);
      var sc = {};
      states.forEach(function (s) { sc[s] = 0; });
      for (var i = 0; i < n; i++) { sc[stateName[i]]++; }
      c1.appendChild(el("h3", "Accounts by state (" + n.toLocaleString("en-US") + ")", "vx-h"));
      var stack = el("div", null, "vx-stack");
      var ordered = ["no_signals", "some_signals", "many_signals", "not_scored"].filter(function (s) { return sc[s] !== undefined; });
      ordered.forEach(function (s) {
        var seg = el("span");
        seg.style.width = (100 * sc[s] / Math.max(1, n)).toFixed(2) + "%";
        seg.style.backgroundColor = safeColor(cfg.state_colors[s]);
        stack.appendChild(seg);
      });
      c1.appendChild(stack);
      ordered.forEach(function (s) { kv(c1, cfg.state_marks[s] + "  " + cfg.state_labels[s], fmtInt(sc[s]), safeColor(cfg.state_colors[s])); });
      c1.appendChild(el("div", "A state describes signals in the data, not a person.", "vx-help"));

      // By verified type (text from X, so textContent only).
      var c2 = $("vx-ov-verified"); clear(c2);
      c2.appendChild(el("h3", "By verification type", "vx-h"));
      var vt = {};
      for (i = 0; i < n; i++) {
        var v = P.verified_type[i];
        var key = (v === null || v === undefined || v === "") ? (missing[i] ? "(no profile)" : "(not in data)") : String(v);
        vt[key] = (vt[key] || 0) + 1;
      }
      var vk = Object.keys(vt).sort(function (a, b) { return vt[b] - vt[a]; });
      var vmax = vk.length ? vt[vk[0]] : 1;
      vk.slice(0, 8).forEach(function (key) { kv(c2, key, fmtInt(vt[key])); bar(c2, vt[key] / vmax); });

      // Interactions by type.
      var c3 = $("vx-ov-types"); clear(c3);
      c3.appendChild(el("h3", "Interactions by type", "vx-h"));
      var tsum = types.map(function (t, k) { var s = 0; for (var j = 0; j < m; j++) { s += counts[k][j] || 0; } return s; });
      var tmax = Math.max.apply(null, tsum.concat([1]));
      types.forEach(function (t, k) {
        kv(c3, TYPE_LABEL[t] || t, fmtInt(tsum[k]), safeColor(cfg.edge_colors[t]), "dash");
        bar(c3, tsum[k] / tmax, safeColor(cfg.edge_colors[t]));
      });
      c3.appendChild(el("div", "Links: " + m.toLocaleString("en-US") + ". Links with no interaction time: " + untimed.toLocaleString("en-US") + ".", "vx-help"));

      // Findings summary.
      var c4 = $("vx-ov-findings"); clear(c4);
      c4.appendChild(el("h3", "Findings (" + F.length + ")", "vx-h"));
      var sevC = { high: 0, notable: 0, info: 0 };
      F.forEach(function (f) { sevC[f.sev] = (sevC[f.sev] || 0) + 1; });
      [["high", "High"], ["notable", "Notable"], ["info", "Information"]].forEach(function (s) {
        var row = kv(c4, s[1], fmtInt(sevC[s[0]]));
        row.firstChild.insertBefore(el("span", s[0], "vx-sev vx-sev-" + s[0]), row.firstChild.firstChild);
        row.firstChild.firstChild.style.marginRight = "8px";
      });
      if (!F.length) { c4.appendChild(el("div", "No findings in this render.", "vx-help")); }
      F.slice(0, 4).forEach(function (f, fi) {
        var row = el("div", null, "vx-kv");
        var b = el("button", f.title, "vx-k vx-isolate");
        b.type = "button";
        b.addEventListener("click", function () { setLeft(true); focusOnFinding(fi); });
        row.appendChild(b);
        row.appendChild(el("span", f.m.length + " accounts", "vx-muted"));
        c4.appendChild(row);
      });

      // Changes since the earlier snapshot.
      var c7 = $("vx-ov-changes"); clear(c7);
      c7.appendChild(el("h3", CH ? "Changes since " + shortDate(CH.base_at) : "Changes", "vx-h"));
      if (!CH) {
        c7.appendChild(el("div", "No earlier snapshot. The next run shows what changed.", "vx-help"));
      } else {
        var cc = CH.counts;
        [["New accounts", cc.new_accounts, "new"], ["Accounts not in this map", cc.gone_accounts, null],
         ["State changed", cc.state_changes, "state"], ["New rules fired", null, "rules"],
         ["Moved to another community", cc.community_moves, "moved"], ["Stronger tie to you", cc.tie_stronger, "tie"],
         ["New findings", cc.new_findings, null]].forEach(function (r) {
          var v = r[1];
          if (v === null) { v = 0; for (var q = 0; q < n; q++) { if (changeOf(q, r[2])) { v++; } } }
          var row = kv(c7, r[0], fmtInt(v));
          if (r[2] && v > 0) {
            var b = el("button", "Show", "vx-btn");
            b.type = "button";
            b.addEventListener("click", function () {
              view.change = r[2]; $("vx-f-change").value = r[2]; setView("map"); applyFilters();
            });
            row.appendChild(b);
          }
        });
      }

      // Top 10 accounts by tie weight to the owner.
      var c5 = $("vx-ov-top"); clear(c5);
      c5.appendChild(el("h3", "Top 10 accounts by tie weight", "vx-h"));
      var top = [];
      for (i = 0; i < n; i++) { if (i !== ego && num(N.ew[i]) > 0) { top.push(i); } }
      top.sort(function (a, b) { return num(N.ew[b]) - num(N.ew[a]) || a - b; });
      top = top.slice(0, 10);
      var emax = top.length ? num(N.ew[top[0]]) : 1;
      if (!top.length) { c5.appendChild(el("div", "No tie weights in the data.", "vx-help")); }
      top.forEach(function (x) {
        var row = el("div", null, "vx-kv");
        var b = el("button", null, "vx-k");
        b.type = "button";
        b.setAttribute("data-node", String(x));
        var sw = el("span", null, "vx-swatch"); sw.style.backgroundColor = safeColor(cfg.state_colors[stateName[x]]);
        b.appendChild(sw);
        b.appendChild(el("span", nameOf(x), "vx-isolate"));
        b.addEventListener("click", function () { goToNode(x); });
        row.appendChild(b);
        row.appendChild(el("span", compact(N.ew[x]), "vx-v"));
        c5.appendChild(row);
        bar(c5, num(N.ew[x]) / emax);
      });

      // Links over time (first interaction of each link, one bar per day).
      var c6 = $("vx-ov-time"); clear(c6);
      c6.appendChild(el("h3", "New links over time", "vx-h"));
      if (!hasTimes) {
        c6.appendChild(el("div", "The data has no interaction times.", "vx-help"));
      } else {
        var days = Math.max(1, Math.ceil((tMax + 1) / 1440));
        var bins = Math.min(days, 90), per = days / bins, hist = new Array(bins).fill(0);
        for (var j = 0; j < m; j++) {
          if (ft[j] === null || ft[j] === undefined) { continue; }
          hist[Math.min(bins - 1, Math.floor(ft[j] / 1440 / per))]++;
        }
        var hmax = Math.max.apply(null, hist.concat([1]));
        var hd = el("div", null, "vx-hist");
        hist.forEach(function (v, b) {
          var s = el("span");
          s.style.height = (100 * v / hmax).toFixed(1) + "%";
          s.title = timeText(Math.floor(b * per * 1440), false) + ": " + v + " new links";
          hd.appendChild(s);
        });
        c6.appendChild(hd);
        var ax = el("div", null, "vx-hist-axis");
        ax.appendChild(el("span", timeText(0, false)));
        ax.appendChild(el("span", timeText(tMax, false)));
        c6.appendChild(ax);
        c6.appendChild(el("div", "Each bar counts the links whose first interaction is in that period.", "vx-help"));
      }
      buildTable();
    }

    var tbl = { key: "ew", dir: -1, q: "" };
    var lowAll = null;
    function sortVal(i, key) {
      switch (key) {
        case "h": return (N.h[i] || N.nm[i] || "").toLocaleLowerCase();
        case "st": return N.st[i] * 1000 + num(N.tp[i]);
        case "ew": return num(N.ew[i]);
        case "din": return num(N.din[i]);
        case "dout": return num(N.dout[i]);
        case "fo": return num(P.followers_count[i]);
        case "age": var a = ageYears(i); return a === null ? -1 : a;
        default: return 0;
      }
    }
    function buildTable() {
      if (!lowAll) {
        lowAll = N.h.map(function (h, i) { return ((h || "") + " " + (N.nm[i] || "")).toLocaleLowerCase(); });
      }
      var q = tbl.q.trim().toLocaleLowerCase();
      if (q.charAt(0) === "@") { q = q.slice(1); }
      var rows = [];
      for (var i = 0; i < n; i++) { if (!q || lowAll[i].indexOf(q) >= 0) { rows.push(i); } }
      rows.sort(function (a, b) {
        var va = sortVal(a, tbl.key), vb = sortVal(b, tbl.key);
        if (va < vb) { return -tbl.dir; }
        if (va > vb) { return tbl.dir; }
        return a - b;
      });
      var LIMIT = 400;
      var tb = $("vx-ov-table").tBodies[0];
      clear(tb);
      rows.slice(0, LIMIT).forEach(function (i) {
        var tr = el("tr");
        tr.setAttribute("data-node", String(i));
        var td = el("td");
        var sw = el("span", null, "vx-swatch"); sw.style.backgroundColor = safeColor(cfg.state_colors[stateName[i]]);
        td.appendChild(sw);
        td.appendChild(el("span", nameOf(i), "vx-isolate"));
        if (N.nm[i] && N.h[i]) { td.appendChild(el("span", "  " + N.nm[i], "vx-muted vx-isolate")); }
        tr.appendChild(td);
        tr.appendChild(el("td", markOf(i) + " " + cfg.state_labels[stateName[i]]));
        tr.appendChild(el("td", compact(N.ew[i]), "vx-num"));
        tr.appendChild(el("td", fmtInt(N.din[i]), "vx-num"));
        tr.appendChild(el("td", fmtInt(N.dout[i]), "vx-num"));
        tr.appendChild(el("td", P.followers_count[i] === null || P.followers_count[i] === undefined ? "-" : fmtInt(P.followers_count[i]), "vx-num"));
        var a = ageYears(i);
        tr.appendChild(el("td", a === null ? "-" : (Math.round(a * 10) / 10) + " y", "vx-num"));
        tr.addEventListener("click", function () { goToNode(i); });
        tb.appendChild(tr);
      });
      $("vx-ov-count").textContent = rows.length > LIMIT
        ? "Showing " + LIMIT + " of " + rows.length.toLocaleString("en-US") + " accounts. Type to narrow the list."
        : rows.length.toLocaleString("en-US") + " accounts";
      Array.prototype.forEach.call($("vx-ov-table").tHead.querySelectorAll("button[data-sort]"), function (b) {
        var on = b.getAttribute("data-sort") === tbl.key;
        b.setAttribute("aria-sort", on ? (tbl.dir > 0 ? "ascending" : "descending") : "none");
        var base = b.textContent.replace(/ [▲▼]$/, "");
        b.textContent = base + (on ? (tbl.dir > 0 ? " ▲" : " ▼") : "");
      });
    }
    function bindOverview() {
      Array.prototype.forEach.call($("vx-ov-table").tHead.querySelectorAll("button[data-sort]"), function (b) {
        b.addEventListener("click", function () {
          var key = b.getAttribute("data-sort");
          if (tbl.key === key) { tbl.dir = -tbl.dir; } else { tbl.key = key; tbl.dir = key === "h" ? 1 : -1; }
          buildTable();
        });
      });
      $("vx-ov-search").addEventListener("input", function (ev) { tbl.q = ev.target.value; buildTable(); });
    }

    // ---- weighted shortest path (FR-47) ----
    function setPathEnd(which, i) {
      if (which === "a") { pathA = i; } else { pathB = i; }
      var t = $(which === "a" ? "vx-path-a" : "vx-path-b");
      t.textContent = markOf(i) + " " + nameOf(i);
      t.className = "vx-isolate";
    }
    function clearPathMarks() {
      if (pathNodes) {
        G.updateEachNodeAttributes(function (k, a) { a.hl = focusSet !== null && focusSet.has(+k); return a; }, { attributes: ["hl"] });
        G.updateEachEdgeAttributes(function (k, a) { a.hl = false; return a; }, { attributes: ["hl"] });
      }
      pathNodes = null;
      clear($("vx-path-out"));
    }
    function findPath() {
      var msg = $("vx-path-msg");
      clearPathMarks();
      if (pathA < 0 || pathB < 0) { msg.textContent = "Select two accounts first (Path from here, Path to here)."; return null; }
      if (pathA === pathB) { msg.textContent = "Select two different accounts."; return null; }
      if (!nodeShown[pathA] || !nodeShown[pathB]) { msg.textContent = "A filter hides one of the two accounts. No path in the shown graph."; return null; }
      // Undirected view of the SHOWN edges. Distance = 1 / combined weight.
      var U = new graphology.Graph({ type: "undirected", multi: false });
      var pairEdges = {};
      for (var j = 0; j < m; j++) {
        if (!edgeShown[j]) { continue; }
        var s = E.s[j], t = E.t[j];
        var a = String(Math.min(s, t)), b = String(Math.max(s, t)), key = a + "|" + b;
        (pairEdges[key] = pairEdges[key] || []).push(j);
      }
      Object.keys(pairEdges).forEach(function (key) {
        var js = pairEdges[key], w = 0;
        if (js.length > 1 && (E.pw[js[0]] || 0) > 0) { w = E.pw[js[0]]; }
        else { js.forEach(function (j) { if ((E.w[j] || 0) > w) { w = E.w[j]; } }); }
        if (!(w > 0)) { return; }
        var ab = key.split("|");
        U.mergeNode(ab[0]); U.mergeNode(ab[1]);
        U.addEdge(ab[0], ab[1], { dist: 1 / w });
      });
      var path = null;
      if (U.hasNode(String(pathA)) && U.hasNode(String(pathB))) {
        path = graphologyLibrary.shortestPath.dijkstra.bidirectional(U, String(pathA), String(pathB), "dist");
      }
      if (!path) {
        msg.textContent = "No path between these two accounts in the shown graph.";
        msg.setAttribute("data-path", "none");
        SigmaView.refresh();
        return null;
      }
      var total = 0;
      pathNodes = path.map(Number);
      var onPath = new Set(path);
      G.updateEachNodeAttributes(function (k, a) { a.hl = onPath.has(k) || (focusSet !== null && focusSet.has(+k)); return a; }, { attributes: ["hl"] });
      var hlEdges = new Set();
      for (var p = 0; p + 1 < path.length; p++) {
        total += U.getEdgeAttribute(U.edge(path[p], path[p + 1]), "dist");
        [[path[p], path[p + 1]], [path[p + 1], path[p]]].forEach(function (st) {
          if (G.hasEdge(st[0], st[1])) { var ek = G.edge(st[0], st[1]); if (edgeShown[+ek.slice(1)]) { hlEdges.add(ek); } }
        });
      }
      G.updateEachEdgeAttributes(function (k, a) { a.hl = hlEdges.has(k); return a; }, { attributes: ["hl"] });
      msg.textContent = "Path with " + (path.length - 1) + " steps. Total distance " + (Math.round(total * 1000) / 1000) + " (distance = 1 / tie weight).";
      msg.setAttribute("data-path", pathNodes.map(function (i) { return N.id[i]; }).join(","));
      var out = $("vx-path-out");
      pathNodes.forEach(function (i) { out.appendChild(el("li", markOf(i) + " " + nameOf(i), "vx-isolate")); });
      SigmaView.refresh();
      return pathNodes;
    }

    // ---- theme ----
    function applyTheme(t) {
      document.body.setAttribute("data-theme", t);
      SigmaView.labelColor = t === "light" ? "#2a2216" : "#f3ecdc";
      drawBackground();
      SigmaView.refresh();
    }

    // ---- start ----
    SigmaView.labelColor = "#f3ecdc";
    if (D.meta.redacted) {
      // --redact render: no handle, name, ID, profile, topic, state, or finding is in this file.
      $("vx-private").textContent = "Redacted file: structure only. No handle, name, ID, profile, or signal is in it.";
      document.body.setAttribute("data-redacted", "1");
    }
    computeEdgeColors();
    computeColors();
    computeSizes();
    buildFacts();
    buildTopBar();
    buildControls();
    buildSearch();
    buildFindings();
    buildPlayback();
    buildLegend();
    computeVisibility();
    drawBackground();
    SigmaView.create($("vx-graph"), G, styles);
    SigmaView.on("clickNode", function (p) { select(+p.node, false); });
    SigmaView.on("enterNode", function (p) { setHover(+p.node); });
    SigmaView.on("leaveNode", function () { setHover(-1); });
    SigmaView.on("nodeDragStart", function (p) { G.setNodeAttribute(p.node, "fixed", true); $("vx-hover").hidden = true; });
    SigmaView.on("nodeDragEnd", function (p) { $("vx-graph").setAttribute("data-dragged", String(p.node)); });
    // Positions only, for the page tests (drag, hover). No value from X.
    window.visualxPositions = {
      viewport: function (i) { var a = G.getNodeAttributes(String(i)); return SigmaView.nodeToViewport(a.x, a.y); },
      graph: function (i) { var a = G.getNodeAttributes(String(i)); return { x: a.x, y: a.y }; }
    };
    $("vx-graph").addEventListener("mousemove", function (ev) {
      var r = $("vx-stage").getBoundingClientRect();
      mouse.x = ev.clientX - r.left; mouse.y = ev.clientY - r.top;
      placeHover();
    });
    $("vx-graph").addEventListener("mouseleave", function () { $("vx-hover").hidden = true; });
    $("vx-graph").addEventListener("mousedown", function () { pointerDown = true; setHover(-1); });
    window.addEventListener("mouseup", function () { pointerDown = false; });

    $("vx-path-go").addEventListener("click", function () { findPath(); });
    $("vx-path-clear").addEventListener("click", function () {
      clearPathMarks(); pathA = -1; pathB = -1;
      $("vx-path-a").textContent = "(none)"; $("vx-path-b").textContent = "(none)";
      var msg = $("vx-path-msg"); msg.textContent = ""; msg.removeAttribute("data-path");
      SigmaView.refresh();
    });
    $("vx-zoom-in").addEventListener("click", function () { SigmaView.zoom(1 / 1.5); });
    $("vx-zoom-out").addEventListener("click", function () { SigmaView.zoom(1.5); });
    $("vx-zoom-fit").addEventListener("click", function () { SigmaView.fit(); });
    $("vx-btn-findings").addEventListener("click", function () { setView("map"); setLeft($("vx-left").hidden); });
    $("vx-left-close").addEventListener("click", function () { setLeft(false); });
    $("vx-btn-controls").addEventListener("click", function () {
      setView("map");
      var showing = !$("vx-right").hidden && $("vx-panel").hidden;
      if (showing) { setRight(false); } else { setRight(true, "controls"); }
    });
    $("vx-right-close").addEventListener("click", function () { setRight(false); deselect(); SigmaView.refresh(); });
    $("vx-tab-account").addEventListener("click", function () { setRight(true, "account"); });
    $("vx-tab-controls").addEventListener("click", function () { setRight(true, "controls"); });
    $("vx-btn-playback").addEventListener("click", function () { setView("map"); setPlayback($("vx-playbar").hidden); });
    $("vx-view-map").addEventListener("click", function () { setView("map"); });
    $("vx-view-overview").addEventListener("click", function () { setView("overview"); });
    $("vx-focus-clear").addEventListener("click", function () { setFocus(null); });
    $("vx-btn-theme").addEventListener("click", function () { applyTheme(document.body.getAttribute("data-theme") === "light" ? "dark" : "light"); });
    function setHelp(open) { $("vx-help").hidden = !open; if (open) { $("vx-help-close").focus(); } }
    $("vx-btn-help").addEventListener("click", function () { setHelp(true); });
    $("vx-help-close").addEventListener("click", function () { setHelp(false); });
    $("vx-help").addEventListener("click", function (ev) { if (ev.target === $("vx-help")) { setHelp(false); } });
    document.addEventListener("keydown", function (ev) {
      var tag = (ev.target && ev.target.tagName) || "";
      if (ev.key === "Escape") {
        if (!$("vx-help").hidden) { setHelp(false); return; }
        if (focusSet) { setFocus(null); }
      }
      if (ev.key === "/" && tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA") { ev.preventDefault(); $("vx-search").focus(); }
      if (ev.key === "?" && tag !== "INPUT") { setHelp(true); }
    });
    window.addEventListener("resize", function () { drawBackground(); });
    buildOverview();
    bindOverview();
    setView("map");
  }

  decodePayload().then(function (D) {
    if (!D || D.v !== 1) { throw new Error("unknown payload version"); }
    new App(D);
    document.body.setAttribute("data-vx-ready", "1");
  }).catch(function (err) {
    showError("The map could not load: " + (err && err.message ? err.message : String(err)));
  });
})();
