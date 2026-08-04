import { PipecatClient, RTVIEvent } from '@pipecat-ai/client-js';
import { SmallWebRTCTransport } from '@pipecat-ai/small-webrtc-transport';

const connectButton = document.querySelector('#connect');
const micButton = document.querySelector('#mic');
const status = document.querySelector('#status');
const orb = document.querySelector('#orb');
const transcript = document.querySelector('#transcript');
const micState = document.querySelector('#mic-state');
const latencyEl = document.querySelector('#latency');
const bargeInEl = document.querySelector('#barge-in');
const rttEl = document.querySelector('#rtt');
const jitterEl = document.querySelector('#jitter');
const lossEl = document.querySelector('#loss');
const audio = document.querySelector('#bot-audio');
const speedSelect = document.querySelector('#speed');
const emotionSelect = document.querySelector('#emotion');
const livePocSelect = document.querySelector('#live-poc');
const playbackState = document.querySelector('#playback-state');
const delegateState = document.querySelector('#delegate-state');
const transcriptStage = document.querySelector('#transcript-stage');
const turnGeneration = document.querySelector('#turn-generation');

let client = null;
let connected = false;
let userStoppedAt = null;
let interruptionStartedAt = null;
let botWasSpeaking = false;
let statsTimer = null;
let connectionGeneration = 0;
let checkpointRunId = null;
let checkpointBuildId = null;
let playbackContext = null;
let playbackMeter = null;
let interruptionMarker = null;
let botStopCallbackMs = null;
let activeBotMessageKey = null;
let activeBotText = '';

function setState(text, className = '') {
  status.textContent = text;
  orb.className = `orb ${className}`.trim();
}

function addMessage(text, role, key = null, stage = 'final') {
  if (!text?.trim()) return;
  transcript.querySelector('.placeholder')?.remove();
  let element = key ? transcript.querySelector(`[data-message-key="${key}"]`) : null;
  if (!element) {
    element = document.createElement('div');
    element.className = `message ${role}`;
    if (key) element.dataset.messageKey = key;
    transcript.appendChild(element);
  }
  element.dataset.stage = stage;
  element.textContent = text;
  transcript.scrollTop = transcript.scrollHeight;
}

function handlePocMessage(data) {
  if (data?.type !== 'fonely-live-poc') return;
  checkpointRunId = data.run_id ?? checkpointRunId;
  checkpointBuildId = data.build_id ?? checkpointBuildId;
  turnGeneration.textContent = `${data.turn_id ?? 0} / ${data.generation_id ?? 0}`;
  if (data.event === 'delegate_finished') delegateState.textContent = data.status;
  if (data.event === 'generation_advanced') {
    delegateState.textContent = 'Cancelled / stale guarded';
    interruptionMarker = performance.now();
    playbackMeter?.port.postMessage({type: 'interrupt'});
  }
  if (data.event === 'transcript_stage') transcriptStage.textContent = data.stage;
}

async function preparePlaybackContext() {
  if (!playbackContext || playbackContext.state === 'closed') {
    playbackContext = new AudioContext();
    await playbackContext.audioWorklet.addModule(new URL('./playback-meter-worklet.js', import.meta.url));
  }
  if (playbackContext.state !== 'running') await playbackContext.resume();
}

async function attachMeasuredPlayback(track) {
  audio.srcObject = new MediaStream([track]);
  await audio.play();
  await preparePlaybackContext();
  const measurementTrack = track.clone();
  const source = playbackContext.createMediaStreamSource(new MediaStream([measurementTrack]));
  playbackMeter = new AudioWorkletNode(playbackContext, 'playback-meter');
  const silentGain = playbackContext.createGain();
  silentGain.gain.value = 0;
  source.connect(playbackMeter).connect(silentGain).connect(playbackContext.destination);
  playbackMeter.port.onmessage = ({data}) => {
    if (data?.type !== 'old-audio-stopped' || interruptionMarker == null) return;
    const outputTimestamp = playbackContext.getOutputTimestamp?.();
    const performanceAtContextZero = outputTimestamp
      ? outputTimestamp.performanceTime - outputTimestamp.contextTime * 1000
      : performance.now() - playbackContext.currentTime * 1000;
    const lastRenderedMs = performanceAtContextZero + data.lastSignalContextMs;
    const latencyMs = Math.max(0, lastRenderedMs - interruptionMarker);
    bargeInEl.textContent = `${Math.round(latencyMs)} ms rendered PCM`;
    client?.sendClientMessage('founder-checkpoint-evidence', {
      run_id: checkpointRunId,
      build_id: checkpointBuildId,
      measurement_method: 'audio-worklet-rms-v1',
      valid: Number.isFinite(latencyMs),
      latency_ms: latencyMs,
      threshold_rms: data.thresholdRms,
      silence_hold_ms: data.silenceHoldMs,
      sample_rate: data.sampleRate,
      base_latency_ms: (playbackContext.baseLatency ?? 0) * 1000,
      output_latency_ms: (playbackContext.outputLatency ?? 0) * 1000,
      bot_stop_callback_ms: botStopCallbackMs,
    });
    interruptionMarker = null;
  };
}

async function collectStats() {
  const pc = client?.transport?.pc;
  if (!pc?.getStats) return;
  const reports = await pc.getStats();
  reports.forEach((report) => {
    if (report.type === 'candidate-pair' && report.state === 'succeeded' && report.currentRoundTripTime != null) {
      rttEl.textContent = `${Math.round(report.currentRoundTripTime * 1000)} ms`;
    }
    if (report.type === 'inbound-rtp' && report.kind === 'audio') {
      if (report.jitter != null) jitterEl.textContent = `${Math.round(report.jitter * 1000)} ms`;
      if (report.packetsLost != null) lossEl.textContent = String(report.packetsLost);
    }
  });
}

async function connect() {
  connectButton.disabled = true;
  setState('Connecting…', 'thinking');
  try {
    await preparePlaybackContext();
  } catch (error) {
    console.warn('Measured playback setup failed:', error);
    playbackContext = null;
  }
  const generation = ++connectionGeneration;
  const isCurrent = () => generation === connectionGeneration;

  client = new PipecatClient({
    transport: new SmallWebRTCTransport(),
    enableMic: true,
    enableCam: false,
    callbacks: {
      onConnected() {
        if (!isCurrent()) return;
        connected = true;
        connectButton.disabled = false;
        connectButton.textContent = 'Disconnect';
        micButton.disabled = false;
        speedSelect.disabled = true;
        livePocSelect.disabled = true;
        delegateState.textContent = livePocSelect.value === 'true' ? 'Idle' : 'Off';
        micState.textContent = 'Live';
        setState('Listening', 'listening');
        statsTimer = setInterval(collectStats, 2000);
      },
      onDisconnected() {
        if (!isCurrent()) return;
        connected = false;
        connectButton.disabled = false;
        connectButton.textContent = 'Connect';
        micButton.disabled = true;
        speedSelect.disabled = false;
        livePocSelect.disabled = false;
        playbackState.textContent = 'Idle';
        micState.textContent = 'Off';
        clearInterval(statsTimer);
        playbackContext?.close();
        playbackContext = null;
        playbackMeter = null;
        setState('Disconnected');
      },
      onUserStartedSpeaking() {
        if (botWasSpeaking) {
          interruptionStartedAt = performance.now();
          setState('Interrupting…', 'user');
        } else {
          setState('You’re speaking', 'user');
        }
      },
      onUserStoppedSpeaking() {
        userStoppedAt = performance.now();
        setState('Understanding…', 'thinking');
      },
      onBotLlmStarted() {
        setState('Thinking…', 'thinking');
      },
      onBotStartedSpeaking() {
        botWasSpeaking = true;
        activeBotMessageKey = `bot-${connectionGeneration}-${performance.now()}`;
        activeBotText = '';
        playbackState.textContent = 'Active · mic still live';
        if (userStoppedAt != null) {
          latencyEl.textContent = `${Math.round(performance.now() - userStoppedAt)} ms`;
          userStoppedAt = null;
        }
        setState('Fonely is speaking', 'bot');
      },
      onBotStoppedSpeaking() {
        if (botWasSpeaking && interruptionStartedAt != null) {
          botStopCallbackMs = performance.now() - interruptionStartedAt;
        }
        if (activeBotMessageKey) {
          transcript.querySelector(`[data-message-key="${activeBotMessageKey}"]`)?.removeAttribute('data-message-key');
        }
        activeBotMessageKey = null;
        activeBotText = '';
        botWasSpeaking = false;
        playbackState.textContent = 'Idle';
        interruptionStartedAt = null;
        setState('Listening', 'listening');
      },
      onUserTranscript(data) {
        const key = `user-${connectionGeneration}-active`;
        transcriptStage.textContent = data.final ? 'final evidence' : 'speculative';
        addMessage(data.text, 'user', key, data.final ? 'final' : 'speculative');
        if (data.final) {
          transcript.querySelector(`[data-message-key="${key}"]`)?.removeAttribute('data-message-key');
        }
      },
      onBotTranscript(data) {
        if (!activeBotMessageKey) activeBotMessageKey = `bot-${connectionGeneration}-${performance.now()}`;
        const incoming = data.text || '';
        activeBotText = incoming.startsWith(activeBotText) ? incoming : activeBotText + incoming;
        addMessage(activeBotText, 'bot', activeBotMessageKey, 'final');
      },
      onError(error) {
        console.error(error);
        setState(error.message || 'Voice error');
      },
      onServerMessage(data) {
        handlePocMessage(data);
      },
    },
  });

  client.on(RTVIEvent.TrackStarted, (track, participant) => {
    if (!participant?.local && track.kind === 'audio') {
      attachMeasuredPlayback(track).catch((error) => {
        console.warn('Measured playback unavailable:', error);
        playbackState.textContent = 'Audio active · PCM metric unavailable';
        audio.srcObject ||= new MediaStream([track]);
        audio.play().catch(() => {
          audio.controls = true;
          audio.style.display = 'block';
          setState('Tap play once to enable audio', 'thinking');
        });
      });
    }
  });

  try {
    await client.startBotAndConnect({
      endpoint: `${location.origin}/start`,
      requestData: {
        transport: 'webrtc',
        createDailyRoom: false,
        enableDefaultIceServers: true,
        body: {
          speed: Number(speedSelect.value),
          emotion: emotionSelect.value,
          live_poc: livePocSelect.value === 'true',
          delegate_delay_ms: Math.min(5000, Math.max(0, Number(new URLSearchParams(location.search).get('delegate_delay_ms')) || 750)),
          checkpoint_run_id: new URLSearchParams(location.search).get('run_id'),
        },
      },
    });
  } catch (error) {
    console.error(error);
    connectButton.disabled = false;
    setState(error.message || 'Connection failed');
  }
}

connectButton.addEventListener('click', async () => {
  if (connected) await client.disconnect();
  else await connect();
});

micButton.addEventListener('click', async () => {
  if (!client) return;
  const enabled = !client.isMicEnabled;
  await client.enableMic(enabled);
  micButton.textContent = enabled ? 'Mute mic' : 'Unmute mic';
  micState.textContent = enabled ? 'Live' : 'Muted';
  setState(enabled ? 'Listening' : 'Mic muted', enabled ? 'listening' : '');
});
