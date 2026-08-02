/* Fonely Voice R&D Lab — Browser Client */

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

let ws = null;
let audioCtx = null;
let micStream = null;
let workletNode = null;
let micActive = false;
let inputMode = 'continuous';
let agentSpeaking = false;
let currentGenerationId = 0;

// Waveform visualization
const waveformCtx = waveformCanvas.getContext('2d');
const rmsHistory = new Array(200).fill(0);

// Audio playback state
let playbackQueue = [];
let isPlaying = false;
let playbackGenerationId = 0;

// Session metrics accumulator
const sessionMetrics = {
  turns: [],
  interruptions: 0,
  falseInterruptions: 0,
};

// --- WebSocket ---

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/voice-lab-ws`);

  ws.onopen = () => {
    setStatus('Connected — tap mic or type to begin');
    micBtn.disabled = false;
    textInput.disabled = false;
    sendBtn.disabled = false;
    ws.send(JSON.stringify({ type: 'greeting' }));
  };

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    handleServerMessage(msg);
  };

  ws.onclose = () => {
    setStatus('Disconnected — reconnecting...');
    micBtn.disabled = true;
    textInput.disabled = true;
    sendBtn.disabled = true;
    setTimeout(connect, 2000);
  };

  ws.onerror = () => {
    setStatus('Connection error');
  };
}

function handleServerMessage(msg) {
  switch (msg.type) {
    case 'session_start':
      setStatus('Session: ' + msg.sessionId.substring(0, 8));
      break;

    case 'stt_ready':
      setStatus('STT ready');
      break;

    case 'tts_ready':
      setStatus('Ready — speak or type');
      break;

    case 'transcript':
      addTranscript(msg.text, msg.role, msg.language);
      if (msg.role === 'assistant') {
        agentSpeaking = true;
        interruptBtn.classList.add('visible');
        setStatus('Agent speaking...');
      }
      break;

    case 'stt_partial':
      updatePartialTranscript(msg.transcript, msg.language);
      break;

    case 'stt_final':
      clearPartialTranscript();
      addTranscript(msg.transcript, 'user', msg.language);
      setStatus('Processing...');
      break;

    case 'audio_chunk':
      if (msg.generationId >= currentGenerationId) {
        queueAudioChunk(msg.audio, msg.sampleRate, msg.generationId);
      }
      break;

    case 'tts_first_audio':
      setStatus('First audio: ' + (msg.latencyMs ? msg.latencyMs.toFixed(0) + 'ms' : '—'));
      break;

    case 'tts_complete':
      agentSpeaking = false;
      interruptBtn.classList.remove('visible');
      setStatus('Ready');
      break;

    case 'interruption':
      currentGenerationId = msg.generationId;
      stopPlayback();
      agentSpeaking = false;
      interruptBtn.classList.remove('visible');
      sessionMetrics.interruptions++;
      addSystemMessage('Interrupted');
      break;

    case 'false_interruption':
      sessionMetrics.falseInterruptions++;
      break;

    case 'silence_prompt':
      setStatus('Silence detected — still there?');
      break;

    case 'turn_metrics':
      displayTurnLatency(msg.metrics);
      sessionMetrics.turns.push(msg.metrics);
      updateSessionMetrics();
      break;

    case 'speakable_plan':
      break;

    case 'safety_triggered':
      addSystemMessage('Safety: ' + msg.safetyType);
      break;

    case 'voice_updated':
      setStatus('Voice: ' + msg.speaker + ' (' + msg.language + ')');
      break;

    case 'session_metrics':
      downloadJSON(msg.metrics, 'voice-lab-metrics.json');
      break;

    case 'stt_error':
    case 'tts_error':
    case 'error':
      setStatus('Error: ' + (msg.message || 'Unknown'));
      break;
  }
}

// --- Audio Capture ---

async function initAudioCapture() {
  if (audioCtx) return;

  audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });

  await audioCtx.audioWorklet.addModule('/voice-capture-worklet.js');

  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      sampleRate: 16000,
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });

  const source = audioCtx.createMediaStreamSource(micStream);
  workletNode = new AudioWorkletNode(audioCtx, 'voice-capture-processor');

  workletNode.port.onmessage = (e) => {
    const data = e.data;

    if (data.type === 'audio' && micActive && ws?.readyState === 1) {
      const base64 = arrayBufferToBase64(data.pcm);
      ws.send(JSON.stringify({ type: 'audio', data: base64 }));
    }

    if (data.type === 'vad' && ws?.readyState === 1) {
      ws.send(JSON.stringify({ type: 'vad', active: data.active }));
    }

    if (data.type === 'rms') {
      rmsHistory.push(data.value);
      rmsHistory.shift();
      drawWaveform();
    }
  };

  source.connect(workletNode);
  workletNode.connect(audioCtx.destination);
}

function startMic() {
  micActive = true;
  micBtn.classList.add('listening');
  workletNode?.port.postMessage({ type: 'start' });
  setStatus('Listening...');
}

function stopMic() {
  micActive = false;
  micBtn.classList.remove('listening');
  workletNode?.port.postMessage({ type: 'stop' });
  if (ws?.readyState === 1) {
    ws.send(JSON.stringify({ type: 'audio_end' }));
  }
  setStatus('Processing...');
}

// --- Audio Playback ---

const playbackCtx = new (window.AudioContext || window.webkitAudioContext)();
let nextPlayTime = 0;

function queueAudioChunk(base64, sampleRate, generationId) {
  if (generationId < playbackGenerationId) return;

  const pcmData = base64ToPCM(base64);
  const audioBuffer = playbackCtx.createBuffer(1, pcmData.length, sampleRate);
  audioBuffer.getChannelData(0).set(pcmData);

  playbackQueue.push({ buffer: audioBuffer, generationId });

  if (!isPlaying) {
    nextPlayTime = playbackCtx.currentTime;
    playNextChunk();
  }
}

function playNextChunk() {
  while (playbackQueue.length > 0) {
    const item = playbackQueue[0];
    if (item.generationId < playbackGenerationId) {
      playbackQueue.shift();
      continue;
    }
    break;
  }

  if (playbackQueue.length === 0) {
    isPlaying = false;
    return;
  }

  isPlaying = true;
  const { buffer, generationId } = playbackQueue.shift();

  if (generationId < playbackGenerationId) {
    playNextChunk();
    return;
  }

  const source = playbackCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(playbackCtx.destination);

  const startTime = Math.max(nextPlayTime, playbackCtx.currentTime);
  source.start(startTime);
  nextPlayTime = startTime + buffer.duration;

  source.onended = () => {
    playNextChunk();
  };
}

function stopPlayback() {
  playbackGenerationId = currentGenerationId;
  playbackQueue = [];
  isPlaying = false;
  nextPlayTime = 0;
}

// --- Waveform Visualization ---

function drawWaveform() {
  const canvas = waveformCanvas;
  const ctx = waveformCtx;
  const dpr = window.devicePixelRatio || 1;

  canvas.width = canvas.offsetWidth * dpr;
  canvas.height = canvas.offsetHeight * dpr;
  ctx.scale(dpr, dpr);

  const w = canvas.offsetWidth;
  const h = canvas.offsetHeight;

  ctx.fillStyle = '#0e0e16';
  ctx.fillRect(0, 0, w, h);

  ctx.strokeStyle = micActive ? '#6366f1' : '#333';
  ctx.lineWidth = 1.5;
  ctx.beginPath();

  const step = w / rmsHistory.length;
  for (let i = 0; i < rmsHistory.length; i++) {
    const x = i * step;
    const amplitude = Math.min(rmsHistory[i] * 10, 1);
    const y = h / 2 - amplitude * (h / 2 - 4);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();

  ctx.beginPath();
  for (let i = 0; i < rmsHistory.length; i++) {
    const x = i * step;
    const amplitude = Math.min(rmsHistory[i] * 10, 1);
    const y = h / 2 + amplitude * (h / 2 - 4);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
}

// --- UI Functions ---

function addTranscript(text, role, language) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = text;
  if (language) {
    const tag = document.createElement('span');
    tag.className = 'lang-tag';
    tag.textContent = language;
    div.appendChild(tag);
  }
  chatArea.appendChild(div);
  chatArea.scrollTop = chatArea.scrollHeight;
}

let partialDiv = null;

function updatePartialTranscript(text, language) {
  if (!partialDiv) {
    partialDiv = document.createElement('div');
    partialDiv.className = 'msg user';
    partialDiv.style.opacity = '0.5';
    chatArea.appendChild(partialDiv);
  }
  partialDiv.textContent = text;
  if (language) {
    const tag = document.createElement('span');
    tag.className = 'lang-tag';
    tag.textContent = language;
    partialDiv.appendChild(tag);
  }
  chatArea.scrollTop = chatArea.scrollHeight;
}

function clearPartialTranscript() {
  if (partialDiv) {
    partialDiv.remove();
    partialDiv = null;
  }
}

function addSystemMessage(text) {
  const div = document.createElement('div');
  div.className = 'msg system-msg';
  div.textContent = text;
  chatArea.appendChild(div);
  chatArea.scrollTop = chatArea.scrollHeight;
}

function setStatus(text) {
  statusLine.textContent = text;
}

function displayTurnLatency(metrics) {
  setLatencyValue('latSTT', metrics.sttLatencyMs);
  setLatencyValue('latLLM', metrics.llmLatencyMs);
  setLatencyValue('latTTS', metrics.ttsFirstAudioMs);
  setLatencyValue('latE2E', metrics.endToEndMs, 1200);
  setLatencyValue('latInt', metrics.interruptionToStopMs, 500);
}

function setLatencyValue(id, value, threshold) {
  const el = document.getElementById(id);
  if (!el) return;

  if (value == null) {
    el.textContent = '—';
    el.className = 'value';
    return;
  }

  el.textContent = Math.round(value) + 'ms';
  el.className = 'value';
  if (threshold) {
    if (value <= threshold * 0.7) el.className = 'value good';
    else if (value <= threshold) el.className = 'value warn';
    else el.className = 'value bad';
  }
}

function updateSessionMetrics() {
  const turns = sessionMetrics.turns;
  document.getElementById('metTurns').textContent = turns.length;

  const e2eValues = turns.map((t) => t.endToEndMs).filter((v) => v != null);
  document.getElementById('metE2Ep50').textContent = percentile(e2eValues, 50);
  document.getElementById('metE2Ep95').textContent = percentile(e2eValues, 95);
  document.getElementById('metInterruptions').textContent = sessionMetrics.interruptions;

  const rate = sessionMetrics.interruptions > 0
    ? ((sessionMetrics.falseInterruptions / sessionMetrics.interruptions) * 100).toFixed(0) + '%'
    : '0%';
  document.getElementById('metFalseRate').textContent = rate;
}

function percentile(values, pct) {
  const sorted = values.filter((v) => v != null).sort((a, b) => a - b);
  if (sorted.length === 0) return '—';
  const idx = Math.ceil((pct / 100) * sorted.length) - 1;
  return Math.round(sorted[Math.max(0, idx)]) + 'ms';
}

// --- Utilities ---

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

function base64ToPCM(base64) {
  const raw = atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  const samples = bytes.length / 2;
  const float32 = new Float32Array(samples);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < samples; i++) {
    float32[i] = view.getInt16(i * 2, true) / 32768;
  }
  return float32;
}

function downloadJSON(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// --- Event Handlers ---

micBtn.addEventListener('click', async () => {
  try {
    await initAudioCapture();
  } catch (err) {
    setStatus('Microphone permission denied');
    return;
  }

  if (inputMode === 'continuous') {
    if (micActive) {
      stopMic();
    } else {
      startMic();
    }
  }
});

// Push-to-talk handlers
micBtn.addEventListener('mousedown', async (e) => {
  if (inputMode !== 'ptt') return;
  e.preventDefault();
  try {
    await initAudioCapture();
    startMic();
  } catch {
    setStatus('Microphone permission denied');
  }
});
micBtn.addEventListener('mouseup', (e) => {
  if (inputMode !== 'ptt') return;
  e.preventDefault();
  stopMic();
});
micBtn.addEventListener('mouseleave', () => {
  if (inputMode !== 'ptt') return;
  if (micActive) stopMic();
});
micBtn.addEventListener('touchstart', async (e) => {
  if (inputMode !== 'ptt') return;
  e.preventDefault();
  try {
    await initAudioCapture();
    startMic();
  } catch {
    setStatus('Microphone permission denied');
  }
});
micBtn.addEventListener('touchend', (e) => {
  if (inputMode !== 'ptt') return;
  e.preventDefault();
  stopMic();
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
textInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendText();
});

function sendText() {
  const text = textInput.value.trim();
  if (!text || !ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: 'text', text }));
  textInput.value = '';
}

voiceSelect.addEventListener('change', () => {
  if (ws?.readyState === 1) {
    ws.send(JSON.stringify({
      type: 'set_voice',
      speaker: voiceSelect.value,
      language: langSelect.value,
      pace: parseFloat(paceSlider.value),
    }));
  }
});

langSelect.addEventListener('change', () => voiceSelect.dispatchEvent(new Event('change')));
paceSlider.addEventListener('input', () => {
  paceValue.textContent = parseFloat(paceSlider.value).toFixed(1);
  voiceSelect.dispatchEvent(new Event('change'));
});

modeContinuous.addEventListener('click', () => {
  inputMode = 'continuous';
  modeContinuous.classList.add('active');
  modePTT.classList.remove('active');
  if (micActive) stopMic();
});

modePTT.addEventListener('click', () => {
  inputMode = 'ptt';
  modePTT.classList.add('active');
  modeContinuous.classList.remove('active');
  if (micActive) stopMic();
});

exportBtn.addEventListener('click', () => {
  if (ws?.readyState === 1) {
    ws.send(JSON.stringify({ type: 'get_metrics' }));
  }
});

// --- Initialize ---

connect();
drawWaveform();
