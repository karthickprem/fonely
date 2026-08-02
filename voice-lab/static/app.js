/* Fonely Voice R&D Lab — Browser */

const chat = document.getElementById('chat');
const statusEl = document.getElementById('status');
const micBtn = document.getElementById('micBtn');
const textInput = document.getElementById('textInput');
const sendBtn = document.getElementById('sendBtn');
const voiceSelect = document.getElementById('voiceSelect');
const langSelect = document.getElementById('langSelect');
const waveCanvas = document.getElementById('waveform');
const waveCtx = waveCanvas.getContext('2d');

let ws = null;
let audioCtx = null;     // created on first user click
let captureCtx = null;    // for mic capture at 16kHz
let workletNode = null;
let recording = false;
let agentSpeaking = false;
let pcmChunks = [];
const rmsHistory = new Array(150).fill(0);

// ---- Playback ----

function ensureAudioCtx() {
  if (!audioCtx) audioCtx = new AudioContext();
  if (audioCtx.state === 'suspended') audioCtx.resume();
}

let playQueue = [];
let playing = false;

function playWav(base64) {
  ensureAudioCtx();
  const binary = atob(base64);
  const buf = new ArrayBuffer(binary.length);
  const view = new Uint8Array(buf);
  for (let i = 0; i < binary.length; i++) view[i] = binary.charCodeAt(i);

  audioCtx.decodeAudioData(buf).then(decoded => {
    playQueue.push(decoded);
    if (!playing) drain();
  }).catch(err => {
    console.error('decodeAudioData failed:', err);
    // Fallback: try as raw PCM 22050Hz
    playRawPCM(base64, 22050);
  });
}

function playRawPCM(base64, sampleRate) {
  ensureAudioCtx();
  const raw = atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  const int16 = new Int16Array(bytes.buffer);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768.0;
  const buf = audioCtx.createBuffer(1, float32.length, sampleRate);
  buf.getChannelData(0).set(float32);
  playQueue.push(buf);
  if (!playing) drain();
}

function drain() {
  if (!playQueue.length) { playing = false; return; }
  playing = true;
  const buf = playQueue.shift();
  const src = audioCtx.createBufferSource();
  src.buffer = buf;
  src.connect(audioCtx.destination);
  src.onended = drain;
  src.start();
}

function stopPlayback() { playQueue = []; playing = false; }

// ---- Mic capture ----

async function initCapture() {
  if (captureCtx) return;
  captureCtx = new AudioContext({ sampleRate: 16000 });
  await captureCtx.audioWorklet.addModule('/static/voice-capture-worklet.js');
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
  });
  const src = captureCtx.createMediaStreamSource(stream);
  workletNode = new AudioWorkletNode(captureCtx, 'voice-capture-processor');
  workletNode.port.onmessage = e => {
    if (e.data.type === 'audio' && recording) {
      pcmChunks.push(new Int16Array(e.data.pcm));
    }
    if (e.data.type === 'rms') {
      rmsHistory.push(e.data.value);
      rmsHistory.shift();
      drawWave();
    }
  };
  src.connect(workletNode);
  workletNode.connect(captureCtx.destination);
}

function startRec() {
  pcmChunks = [];
  recording = true;
  micBtn.classList.add('recording');
  workletNode.port.postMessage({ type: 'start' });
  setStatus('Recording — click mic to stop & send');
}

function stopRecAndSend() {
  recording = false;
  micBtn.classList.remove('recording');
  workletNode.port.postMessage({ type: 'stop' });
  if (!pcmChunks.length) { setStatus('No audio'); return; }

  let total = 0;
  for (const c of pcmChunks) total += c.length;
  const combined = new Int16Array(total);
  let off = 0;
  for (const c of pcmChunks) { combined.set(c, off); off += c.length; }
  pcmChunks = [];

  const bytes = new Uint8Array(combined.buffer);
  const b64 = uint8ToB64(bytes);
  console.log('Sending', bytes.length, 'bytes PCM');
  setStatus('Transcribing...');
  ws.send(JSON.stringify({ type: 'audio_complete', audio: b64 }));
}

// ---- WebSocket ----

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');
  ws.onopen = () => {
    setStatus('Connected — click mic or type');
    micBtn.disabled = false;
    textInput.disabled = false;
    sendBtn.disabled = false;
  };
  ws.onmessage = e => handle(JSON.parse(e.data));
  ws.onclose = () => { setStatus('Disconnected...'); micBtn.disabled = true; setTimeout(connect, 2000); };
}

function handle(msg) {
  switch (msg.type) {
    case 'transcript':
      addMsg(msg.text, msg.role, msg.language);
      break;
    case 'stt_result':
      addMsg(msg.transcript, 'user', msg.language);
      document.getElementById('latSTT').textContent = msg.latencyMs + 'ms';
      setStatus('Thinking...');
      break;
    case 'audio':
      playWav(msg.audio);
      break;
    case 'agent_speaking':
      agentSpeaking = msg.speaking;
      break;
    case 'status':
      setStatus(msg.text);
      break;
    case 'turn_complete':
      document.getElementById('latTotal').textContent = msg.totalMs + 'ms';
      break;
    case 'safety_triggered':
      addSys('Safety: ' + msg.safetyType);
      break;
    case 'voice_updated':
      setStatus('Voice: ' + msg.speaker + ' / ' + msg.language);
      break;
    case 'interrupted':
      stopPlayback();
      agentSpeaking = false;
      break;
    case 'error':
      setStatus(msg.message || 'Error');
      break;
  }
}

// ---- UI ----

function addMsg(text, role, lang) {
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  d.textContent = text;
  if (lang) { const s = document.createElement('span'); s.className = 'lang'; s.textContent = lang; d.appendChild(s); }
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
}

function addSys(text) {
  const d = document.createElement('div');
  d.className = 'msg sys';
  d.textContent = text;
  chat.appendChild(d);
}

function setStatus(t) { statusEl.textContent = t; }

function drawWave() {
  const c = waveCanvas, dpr = devicePixelRatio || 1;
  c.width = c.offsetWidth * dpr; c.height = c.offsetHeight * dpr;
  waveCtx.scale(dpr, dpr);
  const w = c.offsetWidth, h = c.offsetHeight;
  waveCtx.fillStyle = '#0e0e16'; waveCtx.fillRect(0, 0, w, h);
  waveCtx.strokeStyle = recording ? '#6366f1' : '#333'; waveCtx.lineWidth = 1.5;
  const step = w / rmsHistory.length;
  for (const sign of [1, -1]) {
    waveCtx.beginPath();
    for (let i = 0; i < rmsHistory.length; i++) {
      const x = i * step, a = Math.min(rmsHistory[i] * 10, 1);
      const y = h / 2 + sign * a * (h / 2 - 2);
      i === 0 ? waveCtx.moveTo(x, y) : waveCtx.lineTo(x, y);
    }
    waveCtx.stroke();
  }
}

function uint8ToB64(u8) {
  let s = ''; for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]); return btoa(s);
}

// ---- Events ----

micBtn.addEventListener('click', async () => {
  if (agentSpeaking) return;
  ensureAudioCtx();
  try { await initCapture(); } catch { setStatus('Mic denied'); return; }
  if (recording) stopRecAndSend(); else startRec();
});

sendBtn.addEventListener('click', sendText);
textInput.addEventListener('keydown', e => { if (e.key === 'Enter') sendText(); });
function sendText() {
  const t = textInput.value.trim();
  if (!t || !ws || ws.readyState !== 1) return;
  ensureAudioCtx();
  ws.send(JSON.stringify({ type: 'text', text: t }));
  textInput.value = '';
}

voiceSelect.addEventListener('change', updateVoice);
langSelect.addEventListener('change', updateVoice);
function updateVoice() {
  if (ws?.readyState === 1) ws.send(JSON.stringify({ type: 'set_voice', speaker: voiceSelect.value, language: langSelect.value }));
}

connect();
drawWave();
