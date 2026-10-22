'use strict';

const KEY_BUTTONS = {
  KeyJ: 'A', KeyK: 'B', Space: 'Z', Enter: 'START', NumpadEnter: 'START',
  KeyQ: 'L', KeyE: 'R',
  KeyI: 'CU', KeyM: 'CD', KeyU: 'CL', KeyO: 'CR',
  Numpad8: 'CU', Numpad2: 'CD', Numpad4: 'CL', Numpad6: 'CR',
  KeyP: 'PAK',
};
const KEY_DPAD = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right' };
const KEY_STICK = new Set(['KeyW', 'KeyA', 'KeyS', 'KeyD', 'ShiftLeft', 'ShiftRight']);

const PAD_BUTTONS = { 0: 'A', 2: 'B', 1: 'CD', 3: 'CL', 4: 'L', 5: 'R', 9: 'START', 8: 'PAK' };
const PAD_DPAD = { 12: 'up', 13: 'down', 14: 'left', 15: 'right' };
const STICK_DEADZONE = 0.12;
const CSTICK_THRESHOLD = 0.5;
const TRIGGER_THRESHOLD = 0.3;

const SEND_INTERVAL_MS = 100;

const $ = (id) => document.getElementById(id);
const canvas = $('video');
const ctx = canvas.getContext('2d');
const screenEl = $('screen');
const overlay = $('overlay');
const overlayText = $('overlay-text');

let ws = null;
let status = null;
let lastFrameAt = 0;

let myPlayer = 1;
try { myPlayer = Number(localStorage.getItem('bluenetplay.player')) || 1; } catch (e) {  }

const INVITE = new URLSearchParams(location.search).get('invite');
let role = { role: 'host', canChoose: true, canPair: true, relay: false };

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const query = INVITE ? `?invite=${encodeURIComponent(INVITE)}` : '';
  ws = new WebSocket(`${proto}://${location.host}/ws${query}`);
  ws.binaryType = 'arraybuffer';
  setPill('pill-server', 'warn', 'connecting');

  ws.onopen = () => {
    dbg.event('websocket', 'open');
    setPill('pill-server', 'ok', 'connected');
    send({ type: 'player', player: myPlayer });
    if (audio.enabled) send({ type: 'audio', enabled: true });
    lastSent = '';
  };
  ws.onclose = (ev) => {
    dbg.event('websocket', `closed (code ${ev.code}${ev.reason ? `, ${ev.reason}` : ''})`);
    setPill('pill-server', 'bad', 'offline');
    status = null;
    updateOverlay();
    setTimeout(connect, 1500);
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      onJson(JSON.parse(ev.data));
      return;
    }
    const kind = new Uint8Array(ev.data, 0, 1)[0];
    if (kind === 1) onVideo(ev.data);
    else if (kind === 2) onAudio(ev.data);
    else if (kind === 3) onH264(ev.data);
    else if (kind === 4) onOpus(ev.data);
  };
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

let lastStatusSummary = '';
function statusSummary(s) {
  const players = (s.players || []).map((p) => `P${p.player}:${p.state}`).join(' ');
  return `pc:${s.console === false ? 'offline' : 'online'} video:${s.video && s.video.state} ${players}`;
}

function onJson(msg) {
  if (msg.type === 'status') {
    const summary = statusSummary(msg);
    if (summary !== lastStatusSummary) dbg.event('status', summary);
    lastStatusSummary = summary;
    status = msg;
    renderStatus();
  } else if (msg.type === 'hello') {
    role = { role: msg.role || 'host', canChoose: msg.can_choose !== false,
             canPair: msg.can_pair !== false, relay: !!msg.relay, name: msg.name };
    if (!role.canChoose && msg.player) {
      myPlayer = msg.player;
      $('my-player').textContent = String(myPlayer);
    }
    renderRole();
    if (status) renderStatus();
  } else if (msg.type === 'video_config') {
    dbg.event('video-config', `${msg.codec} ${msg.width}x${msg.height}`);
    configureVideo(msg);
  } else if (msg.type === 'audio_config') {
    audioConfig = msg;
    if (audio.enabled) configureOpus();
  } else if (msg.type === 'pong') {
    myRtt = Math.round(performance.now() - msg.t);
    renderPing();
  }
}

setInterval(() => send({ type: 'ping', t: performance.now() }), 2000);

let myRtt = null;
function renderPing() {
  const pc = role.relay && status && status.relay ? status.relay.rtt_ms : null;
  const el = $('pill-ping');
  el.querySelector('em').textContent =
    (myRtt == null ? '-' : `${myRtt} ms`) + (pc != null ? ` + PC ${pc} ms` : '');
  el.title = role.relay
    ? 'Round trip: you <-> server, plus server <-> host. Input and picture each take about half of the total.'
    : 'Round trip browser <-> server';
  const total = (myRtt || 0) + (pc || 0);
  el.classList.toggle('warn', total > 120);
}

function setPill(id, level, text) {
  const el = $(id);
  el.classList.remove('ok', 'warn', 'bad');
  if (level) el.classList.add(level);
  el.querySelector('em').textContent = text;
}

const BT_LABELS = {
  off: ['', 'off'], starting: ['warn', 'starting'], error: ['bad', 'error'],
  waiting: ['warn', 'waiting for BlueRetro'], connecting: ['warn', 'connecting'],
  calibrating: ['warn', 'calibrating'], live: ['ok', 'live'],
};

const PLAYER_LABELS = {
  ...BT_LABELS,
  unpaired: ['', 'not paired'], pairing: ['warn', 'pairing...'], waiting: ['warn', 'waiting for BlueRetro'],
};

function renderStatus() {
  if (!status) return;
  const bt = status.bt;
  const players = status.players || [];
  if (players.length && myPlayer > players.length && myPlayer !== 1) setPlayer(1);
  const mine = players.find((p) => p.player === myPlayer);
  const [lvl, txt] = (myPlayer === 1 || !mine) ? (BT_LABELS[bt.state] || ['', bt.state])
                                               : (PLAYER_LABELS[mine.state] || ['', mine.state]);
  setPill('pill-bt', lvl, `P${myPlayer} ${txt}`);
  $('pill-bt').title = bt.detail + (bt.peer ? ` (${bt.peer})` : '') + ` - profile: ${bt.profile}`;
  renderPlayers(players);
  renderPing();

  const v = status.video;
  if (v.state === 'ok') setPill('pill-video', 'ok', `${v.fps} fps`);
  else setPill('pill-video', v.state === 'error' ? 'bad' : (v.state === 'off' ? '' : 'warn'), v.state);
  $('pill-video').title = v.detail || 'GV-USB2 capture';

  const a = status.audio;
  $('btn-audio').title = a.state === 'ok' ? 'Console sound' : `Audio: ${a.detail || a.state}`;
  updateOverlay();
}

function renderRole() {
  touchEl.classList.toggle('watch', role.role === 'watch');
  const note = $('player-note');
  note.replaceChildren();
  if (role.role === 'watch') {
    note.textContent = 'You are watching (no controller).';
  } else if (!role.canChoose) {
    note.append('You are player ');
    const b = document.createElement('b');
    b.id = 'my-player';
    b.textContent = String(myPlayer);
    note.append(b, role.name ? ` (${role.name}).` : '.');
  } else {
    note.append('This tab controls player ');
    const b = document.createElement('b');
    b.id = 'my-player';
    b.textContent = String(myPlayer);
    note.append(b, '.');
  }
}

function setPlayer(p) {
  if (!role.canChoose || p === myPlayer) return;
  myPlayer = p;
  try { localStorage.setItem('bluenetplay.player', String(p)); } catch (e) {  }
  const label = $('my-player');
  if (label) label.textContent = String(p);
  send({ type: 'player', player: p });
  lastSent = '';
  if (status) renderStatus();
}

function renderPlayers(players) {
  const list = $('player-list');
  list.replaceChildren(...players.map((p) => {
    const [lvl, txt] = (p.player === 1 ? BT_LABELS : PLAYER_LABELS)[p.state] || ['', p.state];
    const row = document.createElement('div');
    row.className = `prow ${lvl}${p.player === myPlayer ? ' mine' : ''}`;
    const dot = document.createElement('i');
    const name = document.createElement('span');
    name.className = 'pname';
    name.textContent = `Player ${p.player}`;
    const st = document.createElement('span');
    st.className = 'pstate';
    st.textContent = txt + (p.tabs ? ` · ${p.tabs} tab${p.tabs > 1 ? 's' : ''}` : '');
    name.append(st);

    const pair = document.createElement('button');
    pair.className = 'tb';
    pair.textContent = 'Pair';
    pair.hidden = p.player === 1 || p.paired || !role.canPair;
    pair.disabled = p.state === 'pairing';
    pair.title = 'Pair this player with BlueRetro (one time)';
    pair.addEventListener('click', (e) => {
      e.currentTarget.blur();
      const ok = confirm(
        `Pair player ${p.player} with BlueRetro?\n\n` +
        'Player 1 will briefly press Z + Start + C-Down: that is BlueRetro\'s ' +
        '"search for controllers" combo. Do this on a menu, not mid-game.\n\n' +
        'Player 1 must be connected, and earlier players paired first.');
      if (ok) send({ type: 'pair', player: p.player });
    });

    const play = document.createElement('button');
    play.className = 'tb';
    play.textContent = p.player === myPlayer ? 'You' : 'Play';
    play.disabled = p.player === myPlayer;
    play.hidden = !role.canChoose && p.player !== myPlayer;
    if (role.role === 'watch') play.hidden = true;
    play.addEventListener('click', (e) => { e.currentTarget.blur(); setPlayer(p.player); });

    row.append(dot, name, pair, play);
    return row;
  }));
}

function updateOverlay() {
  let text = '';
  if (!ws || ws.readyState !== WebSocket.OPEN) text = 'Connecting to the server...';
  else if (status && status.console === false) text = 'The host is offline - waiting for it to connect...';
  else if (status && status.video.state === 'error') text = `No video: ${status.video.detail}`;
  else if (status && status.video.state === 'off') text = 'Video is disabled on the server (--no-video).';
  else if (performance.now() - lastFrameAt > 1500) text = 'Waiting for video...';
  overlay.hidden = !text;
  overlayText.textContent = text;
}
setInterval(updateOverlay, 500);

let decoding = false;
let pendingFrame = null;

function onVideo(buf) {
  pendingFrame = buf;
  if (!decoding) decodeFrames();
}

async function decodeFrames() {
  decoding = true;
  while (pendingFrame) {
    const buf = pendingFrame;
    pendingFrame = null;
    try {
      const bmp = await createImageBitmap(new Blob([new Uint8Array(buf, 1)], { type: 'image/jpeg' }));
      if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
        canvas.width = bmp.width;
        canvas.height = bmp.height;
      }
      ctx.drawImage(bmp, 0, 0);
      bmp.close();
      if (performance.now() - lastFrameAt > 1500) { lastFrameAt = performance.now(); updateOverlay(); }
      lastFrameAt = performance.now();
    } catch (e) {
    }
  }
  decoding = false;
}

const PARAMS = new URLSearchParams(location.search);
const DEBUG = PARAMS.has('debug');
const DECODER_PREF = { software: 'prefer-software', hardware: 'prefer-hardware', auto: 'no-preference' }[PARAMS.get('decoder')]
  || 'prefer-software';
const DROP_PARAM = PARAMS.get('drop');
const DROP_LIMIT = DROP_PARAM === 'off' ? Infinity : (Number(DROP_PARAM) > 0 ? Number(DROP_PARAM) : 30);

let videoDecoder = null;
let videoCfg = null;
let videoSynced = false;

const IS_APPLE = navigator.vendor === 'Apple Computer, Inc.';
let RENDER = ['direct', 'bitmap', 'raf'].includes(PARAMS.get('render')) ? PARAMS.get('render')
  : (IS_APPLE ? 'bitmap' : 'direct');
let nextFrame = null;
let frameBusy = false;
let rafPending = false;

function showFrame(frame) {
  if (RENDER === 'direct') { paintFrame(frame); return; }
  if (nextFrame) { nextFrame.close(); dbg.win.replaced++; }
  nextFrame = frame;
  if (RENDER === 'bitmap') { if (!frameBusy) bitmapFrames(); }
  else if (!rafPending) { rafPending = true; requestAnimationFrame(rafFrame); }
}

function paintFrame(image) {
  const t = performance.now();
  ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
  if (RENDER === 'raf') {
    ctx.fillStyle = 'rgba(0,0,0,0)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
  image.close();
  dbg.onPaint(performance.now() - t);
  if (performance.now() - lastFrameAt > 1500) { lastFrameAt = performance.now(); updateOverlay(); }
  lastFrameAt = performance.now();
}

async function bitmapFrames() {
  frameBusy = true;
  while (nextFrame && RENDER === 'bitmap') {
    const frame = nextFrame;
    nextFrame = null;
    let bmp;
    try {
      bmp = await createImageBitmap(frame);
    } catch (e) {
      dbg.event('render', `createImageBitmap(VideoFrame) failed (${e && e.message || e}), switching to raf`);
      RENDER = 'raf';
      if (!nextFrame) nextFrame = frame; else frame.close();
      rafPending = true;
      requestAnimationFrame(rafFrame);
      break;
    }
    frame.close();
    paintFrame(bmp);
  }
  frameBusy = false;
}

function rafFrame() {
  rafPending = false;
  if (!nextFrame) return;
  const frame = nextFrame;
  nextFrame = null;
  paintFrame(frame);
}

function b64ToBytes(b64) {
  const s = atob(b64 || '');
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

let decoderPref = null;
const rejectedPrefs = new Set();
let configureSeq = 0;

async function pickDecoderPref(base) {
  const order = [...new Set([decoderPref, DECODER_PREF, 'no-preference', 'prefer-hardware', 'prefer-software'])]
    .filter((p) => p && !rejectedPrefs.has(p));
  for (const pref of order) {
    if (!VideoDecoder.isConfigSupported) return pref;
    try {
      const r = await VideoDecoder.isConfigSupported({ ...base, hardwareAcceleration: pref });
      if (r.supported) return pref;
    } catch (e) {  }
    dbg.event('decoder-unsupported', `${base.codec} with ${pref}`);
  }
  return null;
}

async function configureVideo(cfg) {
  videoCfg = cfg;
  if (!('VideoDecoder' in window)) {
    overlay.hidden = false;
    overlayText.textContent = "This browser can't play the stream (it needs WebCodecs: a current Chrome, Edge, Safari or Firefox).";
    return;
  }
  const seq = ++configureSeq;
  if (videoDecoder) { try { videoDecoder.close(); } catch (e) {  } }
  videoDecoder = null;
  videoSynced = false;
  canvas.width = cfg.width;
  canvas.height = cfg.height;
  const base = {
    codec: cfg.codec, codedWidth: cfg.width, codedHeight: cfg.height,
    description: b64ToBytes(cfg.description), optimizeForLatency: true,
  };
  const pref = await pickDecoderPref(base);
  if (seq !== configureSeq) return;
  if (!pref) {
    overlay.hidden = false;
    overlayText.textContent = `This browser can't decode the stream (H.264, ${cfg.codec}). Try Chrome, Edge or Safari.`;
    dbg.event('decoder-none', `nothing supports ${cfg.codec}`);
    return;
  }
  if (pref !== decoderPref && pref !== DECODER_PREF) {
    dbg.event('decoder-fallback', `${DECODER_PREF} not available, using ${pref}`);
  }
  decoderPref = pref;
  const decoder = new VideoDecoder({
    output: (frame) => {
      dbg.onOutput(frame.timestamp, performance.now());
      showFrame(frame);
    },
    error: (e) => {
      console.error('video decoder', e);
      const msg = String(e && e.message || e);
      dbg.event('decoder-error', msg);
      if ((e && e.name === 'NotSupportedError') || /unsupported/i.test(msg)) {
        rejectedPrefs.add(pref);
        if (decoderPref === pref) decoderPref = null;
      }
      if (decoder === videoDecoder) setTimeout(() => videoCfg && configureVideo(videoCfg), 500);
    },
  });
  videoDecoder = decoder;
  const decoderConfig = { ...base, hardwareAcceleration: pref };
  decoder.configure(decoderConfig);
  dbg.onConfigure(cfg, decoderConfig);
}

function onH264(buf) {
  const v = new DataView(buf);
  const key = (v.getUint8(1) & 1) === 1;
  const ts = Number(v.getBigUint64(2));
  dbg.onReceive(ts, key, buf.byteLength);
  if (!videoDecoder || videoDecoder.state !== 'configured') return;
  if (!videoSynced) {
    if (!key) { dbg.skipped++; return; }
    videoSynced = true;
  }
  const queued = videoDecoder.decodeQueueSize;
  dbg.onQueue(queued);
  if (queued > DROP_LIMIT) {
    videoSynced = false;
    dbg.onResync(queued);
    return;
  }
  dbg.onDecode(ts);
  videoDecoder.decode(new EncodedVideoChunk({
    type: key ? 'key' : 'delta', timestamp: ts, data: new Uint8Array(buf, 10),
  }));
}

const dbg = {
  started: Date.now(),
  win: null, history: [], events: [], skipped: 0,
  decodeAt: new Map(),
  lastArrival: null, lastKeyAt: null,
  offsets: [],
  decoderInfo: null, support: null,

  newWindow() {
    return { t: Math.round((Date.now() - this.started) / 1000), recv: 0, bytes: 0, keys: 0, keyBytes: 0,
             maxFrame: 0, decoded: 0, painted: 0, replaced: 0, lat: [], draw: 0, maxQueue: 0, resyncs: 0, maxGap: 0,
             minOffset: Infinity, maxOffset: -Infinity };
  },
  onReceive(ts, key, size) {
    const now = performance.now();
    const w = this.win;
    w.recv++; w.bytes += size; w.maxFrame = Math.max(w.maxFrame, size);
    if (key) { w.keys++; w.keyBytes = Math.max(w.keyBytes, size); this.lastKeyAt = now; }
    if (this.lastArrival != null) {
      const gap = now - this.lastArrival;
      w.maxGap = Math.max(w.maxGap, gap);
      if (gap > 100) this.event('arrival-gap', `${Math.round(gap)} ms without video${key ? ' (then a keyframe)' : ''}`);
    }
    this.lastArrival = now;
    const offset = now - ts / 1000;
    w.minOffset = Math.min(w.minOffset, offset);
    w.maxOffset = Math.max(w.maxOffset, offset);
  },
  onQueue(q) { this.win.maxQueue = Math.max(this.win.maxQueue, q); },
  onDecode(ts) {
    this.decodeAt.set(ts, performance.now());
    if (this.decodeAt.size > 300) this.decodeAt.delete(this.decodeAt.keys().next().value);
  },
  onPaint(drawMs) {
    this.win.painted++;
    this.win.draw = Math.max(this.win.draw, drawMs);
  },
  onOutput(ts, t) {
    const w = this.win;
    w.decoded++;
    const d = this.decodeAt.get(ts);
    if (d != null) {
      w.lat.push(t - d);
      this.decodeAt.delete(ts);
    }
  },
  onResync(q) {
    this.win.resyncs++;
    this.event('drop-to-keyframe', `queue ${q}, ${this.lastKeyAt ? Math.round(performance.now() - this.lastKeyAt) : '?'} ms after last keyframe`);
  },
  onConfigure(cfg, decoderConfig) {
    this.decoderInfo = { codec: cfg.codec, size: `${cfg.width}x${cfg.height}`, hardwareAcceleration: decoderConfig.hardwareAcceleration };
    this.event('configure', JSON.stringify(this.decoderInfo));
    if (VideoDecoder.isConfigSupported) {
      VideoDecoder.isConfigSupported(decoderConfig)
        .then((r) => { this.support = r.supported; })
        .catch(() => { this.support = 'unknown'; });
    }
  },
  event(kind, detail) {
    const last = this.events[this.events.length - 1];
    const t = +((Date.now() - this.started) / 1000).toFixed(1);
    if (last && last.kind === kind && (last.detail === detail || kind === 'slow-decode') && t - last.t < 5) {
      last.repeat = (last.repeat || 1) + 1;
      return;
    }
    this.events.push({ t, kind, detail });
    if (this.events.length > 5000) this.events.shift();
    if (DEBUG) console.debug('[debug]', t, kind, detail);
  },
  rotate() {
    const w = this.win;
    if (w) {
      const lat = w.lat.slice().sort((a, b) => a - b);
      const pct = (p) => lat.length ? Math.round(lat[Math.min(lat.length - 1, Math.floor(p * lat.length))]) : null;
      this.offsets.push(w.minOffset);
      if (this.offsets.length > 30) this.offsets.shift();
      const base = Math.min(...this.offsets.filter(Number.isFinite));
      this.history.push({
        t: w.t, recvFps: w.recv, shownFps: w.decoded, paintedFps: w.painted, replaced: w.replaced, kbit: Math.round(w.bytes * 8 / 1000),
        keyKB: +(w.keyBytes / 1024).toFixed(1), maxFrameKB: +(w.maxFrame / 1024).toFixed(1),
        decodeMs: { p50: pct(0.5), p95: pct(0.95), max: lat.length ? Math.round(lat[lat.length - 1]) : null },
        maxQueue: w.maxQueue, resyncs: w.resyncs, maxGapMs: Math.round(w.maxGap),
        netExtraMs: Number.isFinite(w.minOffset) ? { min: Math.round(w.minOffset - base), max: Math.round(w.maxOffset - base) } : null,
        drawMs: +w.draw.toFixed(1),
        ping: myRtt, pcPing: status && status.relay ? status.relay.rtt_ms : null,
      });
      if (this.history.length > 7200) this.history.shift();
      const h = this.history[this.history.length - 1];
      if (h.decodeMs.p95 != null && h.decodeMs.p95 > 100) {
        this.event('slow-decode', `frames waited ${h.decodeMs.p50}-${h.decodeMs.max} ms in the decoder (p50-max)`);
      }
      if (h.recvFps > 0 && h.shownFps < h.recvFps * 0.8) {
        this.event('frames-lost', `received ${h.recvFps}, shown ${h.shownFps} in 1 s`);
      }
    }
    this.win = this.newWindow();
  },
  report() {
    return {
      when: new Date().toISOString(), userAgent: navigator.userAgent,
      params: { decoder: DECODER_PREF, drop: DROP_PARAM || `default(${DROP_LIMIT})`, render: RENDER, url: location.pathname },
      durationS: Math.round((Date.now() - this.started) / 1000),
      decoder: this.decoderInfo, configSupported: this.support,
      screen: { dpr: devicePixelRatio, canvas: `${canvas.clientWidth}x${canvas.clientHeight}` },
      skippedWaitingForKeyframe: this.skipped,
      history: this.history, events: this.events,
    };
  },
};
dbg.rotate();
setInterval(() => { dbg.rotate(); if (DEBUG) renderDebug(); }, 1000);

document.addEventListener('visibilitychange', () => dbg.event('visibility', document.visibilityState));
window.addEventListener('focus', () => dbg.event('focus', 'window focused'));
window.addEventListener('blur', () => dbg.event('focus', 'window lost focus'));
window.addEventListener('error', (e) => dbg.event('js-error', `${e.message} @ ${e.filename}:${e.lineno}`));
window.addEventListener('unhandledrejection', (e) => dbg.event('js-rejection', String(e.reason && e.reason.message || e.reason)));
for (const level of ['error', 'warn']) {
  const orig = console[level].bind(console);
  console[level] = (...args) => {
    dbg.event(`console-${level}`, args.map((a) => (a && a.message) || String(a)).join(' ').slice(0, 300));
    orig(...args);
  };
}
dbg.event('page', `loaded ${location.pathname} decoder=${DECODER_PREF} drop=${DROP_PARAM || 'default'} render=${RENDER} debug=${DEBUG}`);

function renderDebug() {
  let box = $('debug-box');
  if (!box) {
    box = document.createElement('div');
    box.id = 'debug-box';
    box.className = 'debug-box';
    const pre = document.createElement('pre');
    const download = document.createElement('button');
    download.className = 'tb';
    download.textContent = 'Download log';
    download.title = 'Save the whole session (stats per second + events) as a file';
    download.addEventListener('click', (e) => {
      e.currentTarget.blur();
      const text = JSON.stringify(dbg.report(), null, 1);
      const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
      a.download = `bluenetplay-debug-${stamp}.json`;
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      download.textContent = 'Saved';
      setTimeout(() => { download.textContent = 'Download log'; }, 2000);
    });
    box.append(pre, download);
    screenEl.append(box);
  }
  const h = dbg.history[dbg.history.length - 1];
  if (!h) return;
  const resyncs10 = dbg.history.slice(-10).reduce((n, x) => n + x.resyncs, 0);
  box.querySelector('pre').textContent = [
    `decoder  ${decoderPref || DECODER_PREF}${decoderPref && decoderPref !== DECODER_PREF ? ` (asked ${DECODER_PREF})` : ''}  drop>${DROP_LIMIT}  supported:${dbg.support}  render ${RENDER}`,
    `frames   recv ${h.recvFps}/s  decoded ${h.shownFps}/s  painted ${h.paintedFps}/s  skipped total ${dbg.skipped}`,
    `decode   p50 ${h.decodeMs.p50} ms  p95 ${h.decodeMs.p95}  max ${h.decodeMs.max}`,
    `queue    max ${h.maxQueue}   drops-to-keyframe ${h.resyncs} (last 10 s: ${resyncs10})   duration ${dbg.history.length} s`,
    `network  gap max ${h.maxGapMs} ms  extra delay ${h.netExtraMs ? `${h.netExtraMs.min}-${h.netExtraMs.max}` : '-'} ms`,
    `stream   ${h.kbit} kbit/s  keyframe ${h.keyKB} KB  biggest ${h.maxFrameKB} KB`,
    `ping     ${h.ping ?? '-'} ms + PC ${h.pcPing ?? '-'} ms   draw ${h.drawMs} ms`,
  ].join('\n');
}

let audioConfig = null;
let opusDecoder = null;

function configureOpus() {
  if (!audioConfig || !audio.node || !('AudioDecoder' in window)) return;
  if (opusDecoder) { try { opusDecoder.close(); } catch (e) {  } }
  opusDecoder = new AudioDecoder({
    output: (data) => {
      const n = data.numberOfFrames;
      const l = new Float32Array(n), r = new Float32Array(n);
      data.copyTo(l, { planeIndex: 0, format: 'f32-planar' });
      if (data.numberOfChannels > 1) data.copyTo(r, { planeIndex: 1, format: 'f32-planar' });
      else r.set(l);
      data.close();
      audio.node.port.postMessage({ l, r }, [l.buffer, r.buffer]);
    },
    error: (e) => console.error('audio decoder', e),
  });
  opusDecoder.configure({
    codec: 'opus', sampleRate: 48000, numberOfChannels: 2, description: b64ToBytes(audioConfig.description),
  });
}

function onOpus(buf) {
  if (!audio.enabled || !opusDecoder || opusDecoder.state !== 'configured') return;
  const v = new DataView(buf);
  opusDecoder.decode(new EncodedAudioChunk({
    type: 'key', timestamp: Number(v.getBigUint64(1)), duration: v.getUint32(9), data: new Uint8Array(buf, 13),
  }));
}

const audio = { enabled: false, ctx: null, node: null, gain: null, fallback: false, nextTime: 0 };
const FORCE_FALLBACK = new URLSearchParams(location.search).get('audio') === 'fallback';
const FALLBACK_LEAD = 0.06;

async function setAudio(on) {
  const btn = $('btn-audio');
  try {
    if (on && !audio.ctx) {
      audio.ctx = new AudioContext({ sampleRate: 48000, latencyHint: 'interactive' });
      audio.gain = audio.ctx.createGain();
      audio.gain.gain.value = Number($('volume').value);
      audio.gain.connect(audio.ctx.destination);
      if (audio.ctx.audioWorklet && window.isSecureContext && !FORCE_FALLBACK) {
        await audio.ctx.audioWorklet.addModule('/static/audio-worklet.js');
        audio.node = new AudioWorkletNode(audio.ctx, 'pcm-player', { outputChannelCount: [2] });
        audio.node.connect(audio.gain);
        configureOpus();
      } else {
        audio.fallback = true;
      }
    }
    audio.enabled = on;
    if (audio.ctx) {
      if (on) await audio.ctx.resume(); else await audio.ctx.suspend();
      if (audio.node) audio.node.port.postMessage({ reset: true });
      audio.nextTime = 0;
    }
    send({ type: 'audio', enabled: on });
    btn.title = audio.fallback ? 'Console sound (compatibility player: page is not https)' : 'Console sound';
  } catch (e) {
    audio.enabled = false;
    btn.title = `Sound failed: ${e.message || e}`;
    console.error('audio', e);
  }
  btn.setAttribute('aria-pressed', String(audio.enabled));
  btn.textContent = audio.enabled ? 'Sound on' : 'Sound off';
}

function onAudio(buf) {
  if (!audio.enabled || !audio.ctx) return;
  if (audio.node) {
    const pcm = buf.slice(1);
    audio.node.port.postMessage(pcm, [pcm]);
    return;
  }
  const pcm = new Int16Array(buf.slice(1));
  const frames = pcm.length >> 1;
  const ab = audio.ctx.createBuffer(2, frames, 48000);
  const l = ab.getChannelData(0), r = ab.getChannelData(1);
  for (let i = 0; i < frames; i++) { l[i] = pcm[2 * i] / 32768; r[i] = pcm[2 * i + 1] / 32768; }
  const now = audio.ctx.currentTime;
  if (audio.nextTime < now + 0.01 || audio.nextTime > now + 0.3) audio.nextTime = now + FALLBACK_LEAD;
  const src = audio.ctx.createBufferSource();
  src.buffer = ab;
  src.connect(audio.gain);
  src.start(audio.nextTime);
  audio.nextTime += ab.duration;
}

$('btn-audio').addEventListener('click', (e) => { e.currentTarget.blur(); setAudio(!audio.enabled); });
$('volume').addEventListener('input', (e) => { if (audio.gain) audio.gain.gain.value = Number(e.target.value); });

$('btn-fullscreen').addEventListener('click', (e) => {
  e.currentTarget.blur();
  if (document.fullscreenElement) document.exitFullscreen();
  else screenEl.requestFullscreen().catch(() => {});
});
screenEl.addEventListener('dblclick', () => { if (!playing) $('btn-fullscreen').click(); });

const MIN_PRESS_MS = 60;
const keysDown = new Set();
const pressedAt = new Map();

function isTypingTarget(t) {
  return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
}

function isMapped(code) {
  return code in KEY_BUTTONS || code in KEY_DPAD || KEY_STICK.has(code);
}

window.addEventListener('keydown', (e) => {
  if (!isMapped(e.code) || isTypingTarget(e.target) || e.ctrlKey || e.altKey || e.metaKey) return;
  e.preventDefault();
  if (!keysDown.has(e.code)) pressedAt.set(e.code, performance.now());
  keysDown.add(e.code);
  updateInput();
}, true);
window.addEventListener('keyup', (e) => {
  if (!keysDown.has(e.code)) return;
  e.preventDefault();
  const downAt = pressedAt.get(e.code);
  const left = MIN_PRESS_MS - (performance.now() - downAt);
  const release = () => {
    if (pressedAt.get(e.code) === downAt) {
      keysDown.delete(e.code);
      pressedAt.delete(e.code);
      updateInput();
    }
  };
  if (left > 0) setTimeout(release, left); else release();
}, true);

function releaseAll() { keysDown.clear(); pressedAt.clear(); updateInput(); }
window.addEventListener('blur', releaseAll);
document.addEventListener('visibilitychange', () => { if (document.hidden) releaseAll(); });

screenEl.addEventListener('click', () => screenEl.focus());
function updateFocusHint() {
  screenEl.classList.toggle('focused', document.hasFocus());
}
window.addEventListener('focus', updateFocusHint);
window.addEventListener('blur', updateFocusHint);
updateFocusHint();

const TOUCH_CAPABLE = matchMedia('(pointer: coarse)').matches || navigator.maxTouchPoints > 0
  || PARAMS.get('touch') === '1';
const touchEl = $('touch');
const touch = {
  held: new Map(),
  pressedAt: new Map(),
  on: new Set(),
  stickId: null, stickX: 0, stickY: 0,
};
const STICK_TOUCH_DEADZONE = 0.08;

function touchPress(el, pointerId) {
  let set = touch.held.get(el);
  if (!set) touch.held.set(el, set = new Set());
  const wasDown = set.size > 0;
  set.add(pointerId);
  if (!wasDown) {
    touch.pressedAt.set(el, performance.now());
    touch.on.add(el);
    el.classList.add('on');
    if (navigator.vibrate) navigator.vibrate(8);
    updateInput();
  }
}

function touchRelease(el, pointerId) {
  const set = touch.held.get(el);
  if (!set || !set.delete(pointerId) || set.size) return;
  const downAt = touch.pressedAt.get(el);
  const finish = () => {
    if ((touch.held.get(el) || new Set()).size) return;
    touch.on.delete(el);
    el.classList.remove('on');
    updateInput();
  };
  const left = MIN_PRESS_MS - (performance.now() - downAt);
  if (left > 0) setTimeout(finish, left); else finish();
}

for (const el of touchEl.querySelectorAll('.t-btn')) {
  el.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    try {
      if (el.hasPointerCapture && el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId);
    } catch (err) {  }
    touchPress(el, e.pointerId);
  });
  el.addEventListener('pointerenter', (e) => {
    if (e.pointerType !== 'mouse' && e.pressure > 0) touchPress(el, e.pointerId);
  });
  for (const type of ['pointerup', 'pointercancel', 'pointerleave']) {
    el.addEventListener(type, (e) => touchRelease(el, e.pointerId));
  }
}

const stickZone = $('t-stick-zone');
const stickBase = $('t-stick-base');
const stickKnob = $('t-stick-knob');
let stickOrigin = null;

function stickRadius() { return stickBase.offsetWidth * 0.42; }

function moveStick(e) {
  const r = stickRadius();
  let dx = e.clientX - stickOrigin.x;
  let dy = e.clientY - stickOrigin.y;
  const dist = Math.hypot(dx, dy);
  if (dist > r) { dx *= r / dist; dy *= r / dist; }
  stickKnob.style.transform = `translate(${dx}px, ${dy}px)`;
  let x = dx / r, y = dy / r;
  if (Math.hypot(x, y) < STICK_TOUCH_DEADZONE) { x = 0; y = 0; }
  touch.stickX = x;
  touch.stickY = y;
  updateInput();
}

stickZone.addEventListener('pointerdown', (e) => {
  if (touch.stickId !== null) return;
  e.preventDefault();
  touch.stickId = e.pointerId;
  try { stickZone.setPointerCapture(e.pointerId); } catch (err) {  }
  const zone = stickZone.getBoundingClientRect();
  const half = stickBase.offsetWidth / 2;
  const cx = Math.min(Math.max(e.clientX, zone.left + half), zone.right - half);
  const cy = Math.min(Math.max(e.clientY, zone.top + half), zone.bottom - half);
  stickBase.style.transform = '';
  const rest = stickBase.getBoundingClientRect();
  stickBase.style.transform = `translate(${cx - rest.left - half}px, ${cy - rest.top - half}px)`;
  stickBase.classList.add('active');
  stickOrigin = { x: cx, y: cy };
  if (navigator.vibrate) navigator.vibrate(5);
  moveStick(e);
});
stickZone.addEventListener('pointermove', (e) => { if (e.pointerId === touch.stickId) moveStick(e); });
function endStick(e) {
  if (e && e.pointerId !== touch.stickId) return;
  touch.stickId = null;
  touch.stickX = touch.stickY = 0;
  stickOrigin = null;
  stickKnob.style.transform = '';
  stickBase.style.transform = '';
  stickBase.classList.remove('active');
  updateInput();
}
stickZone.addEventListener('pointerup', endStick);
stickZone.addEventListener('pointercancel', endStick);

function touchReleaseAll() {
  for (const el of touch.on) el.classList.remove('on');
  touch.held.clear(); touch.on.clear(); touch.pressedAt.clear();
  if (touch.stickId !== null) endStick({ pointerId: touch.stickId });
}
touchEl.addEventListener('contextmenu', (e) => e.preventDefault());
window.addEventListener('blur', touchReleaseAll);
document.addEventListener('visibilitychange', () => { if (document.hidden) touchReleaseAll(); });

let playing = false;

async function enterPlay() {
  if (playing) return;
  playing = true;
  document.body.classList.add('playing');
  touchEl.hidden = false;
  touchEl.classList.toggle('watch', role.role === 'watch');
  try {
    if (document.documentElement.requestFullscreen && !document.fullscreenElement) {
      await document.documentElement.requestFullscreen({ navigationUI: 'hide' });
    }
  } catch (e) {  }
  try { if (screen.orientation && screen.orientation.lock) await screen.orientation.lock('landscape'); } catch (e) {  }
  if (!audio.enabled) setAudio(true);
  dbg.event('touch', 'entered play mode');
}

async function exitPlay() {
  if (!playing) return;
  playing = false;
  touchReleaseAll();
  document.body.classList.remove('playing');
  touchEl.hidden = true;
  try { if (screen.orientation && screen.orientation.unlock) screen.orientation.unlock(); } catch (e) {  }
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  updateInput();
  dbg.event('touch', 'left play mode');
}

$('t-exit').addEventListener('click', (e) => {
  e.stopPropagation();
  exitPlay();
});
$('btn-play').addEventListener('click', (e) => { e.currentTarget.blur(); enterPlay(); });
if (TOUCH_CAPABLE) {
  $('btn-play').hidden = false;
  $('focus-hint').textContent = 'Tap the screen to play';
  screenEl.addEventListener('click', () => { if (!playing) enterPlay(); });
}

let lastSourceLabel = '';

function readInput() {
  const buttons = new Set();
  const dpad = new Set();
  let x = 0, y = 0;
  const sources = ['Keyboard'];

  for (const code of keysDown) {
    if (code in KEY_BUTTONS) buttons.add(KEY_BUTTONS[code]);
    if (code in KEY_DPAD) dpad.add(KEY_DPAD[code]);
  }
  const reach = (keysDown.has('ShiftLeft') || keysDown.has('ShiftRight')) ? 0.5 : 1;
  const kx = (keysDown.has('KeyD') ? 1 : 0) - (keysDown.has('KeyA') ? 1 : 0);
  const ky = (keysDown.has('KeyS') ? 1 : 0) - (keysDown.has('KeyW') ? 1 : 0);
  if (kx || ky) { x = kx * reach; y = ky * reach; }

  if (playing) {
    sources.push('Touch');
    for (const el of touch.on) {
      if (el.dataset.btn) buttons.add(el.dataset.btn);
      if (el.dataset.dir) dpad.add(el.dataset.dir);
    }
    if (touch.stickId !== null && !(kx || ky)) { x = touch.stickX; y = touch.stickY; }
  }

  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  for (const gp of pads) {
    if (!gp || !gp.connected) continue;
    sources.push(gp.id.replace(/\s*\(.*\)\s*$/, '').slice(0, 32) || 'Gamepad');
    const pressed = (i) => gp.buttons[i] && gp.buttons[i].pressed;
    for (const [i, name] of Object.entries(PAD_BUTTONS)) if (pressed(i)) buttons.add(name);
    for (const [i, dir] of Object.entries(PAD_DPAD)) if (pressed(i)) dpad.add(dir);
    const trig = (i) => gp.buttons[i] && gp.buttons[i].value > TRIGGER_THRESHOLD;
    if (trig(6) || trig(7)) buttons.add('Z');

    const ax = gp.axes[0] || 0, ay = gp.axes[1] || 0;
    const mag = Math.hypot(ax, ay);
    if (mag > STICK_DEADZONE && !(kx || ky) && touch.stickId === null) {
      const scaled = Math.min(1, (mag - STICK_DEADZONE) / (1 - STICK_DEADZONE)) / mag;
      x = ax * scaled;
      y = ay * scaled;
    }
    const rx = gp.axes[2] || 0, ry = gp.axes[3] || 0;
    if (ry < -CSTICK_THRESHOLD) buttons.add('CU');
    if (ry > CSTICK_THRESHOLD) buttons.add('CD');
    if (rx < -CSTICK_THRESHOLD) buttons.add('CL');
    if (rx > CSTICK_THRESHOLD) buttons.add('CR');
  }

  const label = sources.join(' + ');
  if (label !== lastSourceLabel) $('input-src').textContent = lastSourceLabel = label;
  return {
    buttons: [...buttons].sort(),
    dpad: [...dpad].sort(),
    x: Math.round(x * 1000) / 1000,
    y: Math.round(y * 1000) / 1000,
  };
}

const VIS_IDS = ['A', 'B', 'Z', 'START', 'L', 'R', 'CU', 'CD', 'CL', 'CR'];
function renderPad(state) {
  for (const id of VIS_IDS) $('v-' + id).classList.toggle('on', state.buttons.includes(id));
  for (const dir of ['up', 'down', 'left', 'right']) $('v-' + dir).classList.toggle('on', state.dpad.includes(dir));
  const knob = $('v-stick');
  knob.setAttribute('cx', 160 + state.x * 13);
  knob.setAttribute('cy', 132 + state.y * 13);
  knob.classList.toggle('moved', state.x !== 0 || state.y !== 0);
  $('stick-readout').textContent =
    `Stick ${Math.round(state.x * 100)}%, ${Math.round(-state.y * 100)}%` +
    (state.buttons.includes('PAK') ? '  |  Pak toggle' : '');
}

let lastSent = '';
let lastSentAt = 0;
let lastState = null;

function updateInput() {
  if (role.role === 'watch') return;
  const state = readInput();
  const key = JSON.stringify(state);
  const now = performance.now();
  if (key !== lastSent || now - lastSentAt > SEND_INTERVAL_MS) {
    send({ type: 'input', ...state });
    lastSent = key;
    lastSentAt = now;
  }
  lastState = state;
}

function renderLoop() {
  if (lastState) renderPad(lastState);
  requestAnimationFrame(renderLoop);
}

$('my-player').textContent = String(myPlayer);
connect();
setInterval(updateInput, 8);
requestAnimationFrame(renderLoop);
