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
const voiceSelect = document.querySelector('#voice');
const paceSelect = document.querySelector('#pace');
const temperatureSelect = document.querySelector('#temperature');

let client = null;
let connected = false;
let userStoppedAt = null;
let interruptionStartedAt = null;
let botWasSpeaking = false;
let statsTimer = null;
let connectionGeneration = 0;

function setState(text, className = '') {
  status.textContent = text;
  orb.className = `orb ${className}`.trim();
}

function addMessage(text, role) {
  if (!text?.trim()) return;
  transcript.querySelector('.placeholder')?.remove();
  const element = document.createElement('div');
  element.className = `message ${role}`;
  element.textContent = text;
  transcript.appendChild(element);
  transcript.scrollTop = transcript.scrollHeight;
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
        voiceSelect.disabled = true;
        paceSelect.disabled = true;
        temperatureSelect.disabled = true;
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
        voiceSelect.disabled = false;
        paceSelect.disabled = false;
        temperatureSelect.disabled = false;
        micState.textContent = 'Off';
        clearInterval(statsTimer);
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
        if (userStoppedAt != null) {
          latencyEl.textContent = `${Math.round(performance.now() - userStoppedAt)} ms`;
          userStoppedAt = null;
        }
        setState('Fonely is speaking', 'bot');
      },
      onBotStoppedSpeaking() {
        if (botWasSpeaking && interruptionStartedAt != null) {
          bargeInEl.textContent = `${Math.round(performance.now() - interruptionStartedAt)} ms`;
        }
        botWasSpeaking = false;
        interruptionStartedAt = null;
        setState('Listening', 'listening');
      },
      onUserTranscript(data) {
        if (data.final) addMessage(data.text, 'user');
      },
      onBotTranscript(data) {
        addMessage(data.text, 'bot');
      },
      onError(error) {
        console.error(error);
        setState(error.message || 'Voice error');
      },
    },
  });

  client.on(RTVIEvent.TrackStarted, (track, participant) => {
    if (!participant?.local && track.kind === 'audio') {
      audio.srcObject = new MediaStream([track]);
      audio.play().catch((error) => {
        console.warn('Autoplay blocked:', error);
        audio.controls = true;
        audio.style.display = 'block';
        setState('Tap the audio control once', 'thinking');
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
          voice: voiceSelect.value,
          pace: Number(paceSelect.value),
          temperature: Number(temperatureSelect.value),
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
