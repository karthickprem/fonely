/* Fonely Voice R&D Lab — Browser */

const chatArea = document.getElementById('chatArea');
const statusLine = document.getElementById('statusLine');
const micBtn = document.getElementById('micBtn');
const interruptBtn = document.getElementById('interruptBtn');
const textInput = document.getElementById('textInput');
const sendBtn = document.getElementById('sendBtn');
const voiceSelect = document.getElementById('voiceSelect');
const langSelect = document.getElementById('langSelect');
const paceSlider = document.getElementById('paceSlider');
const paceValue = document.getElementById('paceValue');
const waveformCanvas = document.getElementById('waveformCanvas');
const exportBtn = document.getElementById('exportBtn');
const modeContinuous = document.getElementById('modeContinuous');
const modePTT = document.getElementById('modePTT');
const waveCtx = waveformCanvas.getContext('2d');

let ws = null;
let audioCtx = null;  // created on first user gesture
let captureCtx = null;
let workletNode = null;
let micActive = false;
let agentSpeaking = false;
let pcmChunks = [];    // collected during recording
const rmsHistory = new Array(200).fill(0);
const sessionMetrics = { turns: [], interruptions: 0 };

// ---- WebSocket ----

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/voice-lab-ws');

  ws.onopen = () => {
    status('Connected — click mic or type');
    micBtn.disabled = false;
    textInput.disabled = false;
    sendBtn.disabled = false;
    ws.send(JSON.stringify({ type: 'greeting' }));
  };

  ws.onmessage = (e) => handleMsg(JSON.parse(e.data));

  ws.onclose = () => {
    status('Disconnected — reconnecting...');
    micBtn.disabled = true;
    setTimeout(connect, 2000);
  };
}

function handleMsg(msg) {
  switch (msg.type) {
    case 'session_start':
      break;

    case 'status':
      status(msg.text);
      break;

    case 'transcript':
      addChat(msg.text, msg.role, msg.language);
      break;

    case 'stt_result':
      addChat(msg.transcript, 'user', msg.language);
      status('Thinking...');
      break;

    case 'agent_speaking':
      agentSpeaking = msg.speaking;
      interruptBtn.classList.toggle('visible', msg.speaking);
      if (msg.speaking) status('Speaking...');
      break;

    case 'audio':
      playPCM(msg.audio, msg.sampleRate || 22050);
      break;

    case 'audio_done':
      break;

    case 'interrupted':
      stopPlayback();
      agentSpeaking = false;
      interruptBtn.classList.remove('visible');
      sessionMetrics.interruptions++;
      break;

    case 'turn_metrics':
      showLatency(msg.metrics);
      sessionMetrics.turns.push(msg.metrics);
      updateMetrics();
      break;

    case 'safety_triggered':
      addSystem('Safety: ' + msg.safetyType);
      break;

    case 'voice_updated':
      status('Voice: ' + msg.speaker + ' / ' + msg.language);
      break;

    case 'session_metrics':
      downloadJSON(msg.metrics, 'voice-lab-metrics.json');
      break;

    case 'error':
    case 'stt_error':
    case 'tts_error':
      status(msg.message || 'Error');
      break;
  }
}

// ---- Audio capture ----

async function initCapture() {
  if (captureCtx) return;
  captureCtx = new AudioContext({ sampleRate: 16000 });
  await captureCtx.audioWorklet.addModule('/voice-capture-worklet.js');

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true,
             noiseSuppression: true, autoGainControl: true }
  });

  const src = captureCtx.createMediaStreamSource(stream);
  workletNode = new AudioWorkletNode(captureCtx, 'voice-capture-processor');

  workletNode.port.onmessage = (e) => {
    if (e.data.type === 'audio' && micActive) {
      pcmChunks.push(new Int16Array(e.data.pcm));
    }
    if (e.data.type === 'rms') {
      rmsHistory.push(e.data.value);
      rmsHistory.shift();
      drawWaveform();
    }
  };

  src.connect(workletNode);
  workletNode.connect(captureCtx.destination);
}

function startRecording() {
  pcmChunks = [];
  micActive = true;
  micBtn.classList.add('listening');
  workletNode.port.postMessage({ type: 'start' });
  status('Recording — click mic to stop');
}

function stopRecordingAndSend() {
  micActive = false;
  micBtn.classList.remove('listening');
  workletNode.port.postMessage({ type: 'stop' });

  if (pcmChunks.length === 0) {
    status('No audio recorded');
    return;
  }

  // Combine all PCM chunks into one buffer
  const totalSamples = pcmChunks.reduce((n, c) => n + c.length, 0);
  const combined = new Int16Array(totalSamples);
  let offset = 0;
  for (const chunk of pcmChunks) {
    combined.set(chunk, offset);
    offset += chunk.length;
  }
  pcmChunks = [];

  // Convert to base64 and send
  const bytes = new Uint8Array(combined.buffer);
  const base64 = uint8ToBase64(bytes);

  console.log('Sending audio:', combined.length, 'samples,', bytes.length, 'bytes');
  status('Transcribing...');
  ws.send(JSON.stringify({ type: 'audio_complete', audio: base64 }));
}

// ---- Audio playback ----

let playQueue = [];
let playing = false;

function ensureAudioCtx() {
  if (!audioCtx) audioCtx = new AudioContext();
  if (audioCtx.state === 'suspended') audioCtx.resume();
}

function playPCM(base64, sampleRate) {
  ensureAudioCtx();

  // Decode base64 → Int16 → Float32
  const raw = atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);

  const int16 = new Int16Array(bytes.buffer);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768.0;

  const buf = audioCtx.createBuffer(1, float32.length, sampleRate);
  buf.getChannelData(0).set(float32);

  playQueue.push(buf);
  if (!playing) drainPlayQueue();
}

function drainPlayQueue() {
  if (playQueue.length === 0) { playing = false; return; }
  playing = true;

  const buf = playQueue.shift();
  const src = audioCtx.createBufferSource();
  src.buffer = buf;
  src.connect(audioCtx.destination);
  src.onended = () => drainPlayQueue();
  src.start();
}

function stopPlayback() {
  playQueue = [];
  playing = false;
}

// ---- Waveform ----

function drawWaveform() {
  const c = waveformCanvas;
  const dpr = devicePixelRatio || 1;
  c.width = c.offsetWidth * dpr;
  c.height = c.offsetHeight * dpr;
  waveCtx.scale(dpr, dpr);
  const w = c.offsetWidth, h = c.offsetHeight;

  waveCtx.fillStyle = '#0e0e16';
  waveCtx.fillRect(0, 0, w, h);
  waveCtx.strokeStyle = micActive ? '#6366f1' : '#333';
  waveCtx.lineWidth = 1.5;

  const step = w / rmsHistory.length;
  for (const sign of [1, -1]) {
    waveCtx.beginPath();
    for (let i = 0; i < rmsHistory.length; i++) {
      const x = i * step;
      const a = Math.min(rmsHistory[i] * 10, 1);
      const y = h / 2 + sign * a * (h / 2 - 4);
      i === 0 ? waveCtx.moveTo(x, y) : waveCtx.lineTo(x, y);
    }
    waveCtx.stroke();
  }
}

// ---- UI helpers ----

function addChat(text, role, lang) {
  const div = document.createElement('div');
  div.className = 'msg ' + (role === 'user' ? 'user' : 'assistant');
  div.textContent = text;
  if (lang) {
    const tag = document.createElement('span');
    tag.className = 'lang-tag';
    tag.textContent = lang;
    div.appendChild(tag);
  }
  chatArea.appendChild(div);
  chatArea.scrollTop = chatArea.scrollHeight;
}

function addSystem(text) {
  const div = document.createElement('div');
  div.className = 'msg system-msg';
  div.textContent = text;
  chatArea.appendChild(div);
  chatArea.scrollTop = chatArea.scrollHeight;
}

function status(text) { statusLine.textContent = text; }

function showLatency(m) {
  setLat('latSTT', m.sttLatencyMs);
  setLat('latLLM', m.llmLatencyMs);
  setLat('latTTS', m.ttsFirstAudioMs);
  setLat('latE2E', m.endToEndMs, 1200);
  setLat('latInt', m.interruptionToStopMs, 500);
}

function setLat(id, v, threshold) {
  const el = document.getElementById(id);
  if (!el) return;
  if (v == null) { el.textContent = '—'; el.className = 'value'; return; }
  el.textContent = Math.round(v) + 'ms';
  el.className = 'value';
  if (threshold) {
    el.classList.add(v <= threshold * 0.7 ? 'good' : v <= threshold ? 'warn' : 'bad');
  }
}

function updateMetrics() {
  const t = sessionMetrics.turns;
  document.getElementById('metTurns').textContent = t.length;
  const e2e = t.map(x => x.endToEndMs).filter(x => x != null).sort((a,b) => a - b);
  document.getElementById('metE2Ep50').textContent = pct(e2e, 50);
  document.getElementById('metE2Ep95').textContent = pct(e2e, 95);
  document.getElementById('metInterruptions').textContent = sessionMetrics.interruptions;
}

function pct(arr, p) {
  if (!arr.length) return '—';
  return Math.round(arr[Math.min(Math.ceil(p / 100 * arr.length) - 1, arr.length - 1)]) + 'ms';
}

function uint8ToBase64(u8) {
  let s = '';
  for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
  return btoa(s);
}

function downloadJSON(data, name) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)]));
  a.download = name;
  a.click();
}

// ---- Event handlers ----

micBtn.addEventListener('click', async () => {
  if (agentSpeaking || !ws || ws.readyState !== 1) return;
  ensureAudioCtx();
  try { await initCapture(); } catch { status('Mic permission denied'); return; }

  if (micActive) {
    stopRecordingAndSend();
  } else {
    startRecording();
  }
});

interruptBtn.addEventListener('click', () => {
  if (ws?.readyState === 1) {
    ws.send(JSON.stringify({ type: 'interrupt' }));
    stopPlayback();
    agentSpeaking = false;
    interruptBtn.classList.remove('visible');
  }
});

sendBtn.addEventListener('click', sendText);
textInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendText(); });

function sendText() {
  const t = textInput.value.trim();
  if (!t || !ws || ws.readyState !== 1) return;
  ensureAudioCtx();
  ws.send(JSON.stringify({ type: 'text', text: t }));
  textInput.value = '';
}

voiceSelect.addEventListener('change', updateVoice);
langSelect.addEventListener('change', updateVoice);
paceSlider.addEventListener('input', () => {
  paceValue.textContent = parseFloat(paceSlider.value).toFixed(1);
});

function updateVoice() {
  if (ws?.readyState === 1) {
    ws.send(JSON.stringify({
      type: 'set_voice',
      speaker: voiceSelect.value,
      language: langSelect.value,
    }));
  }
}

exportBtn.addEventListener('click', () => {
  if (ws?.readyState === 1) ws.send(JSON.stringify({ type: 'get_metrics' }));
});

// ---- Start ----
connect();
drawWaveform();
