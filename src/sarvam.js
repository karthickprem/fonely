import { WebSocket } from 'ws';

const SARVAM_API_KEY = process.env.SARVAM_API_KEY;
const SARVAM_TTS_URL = 'https://api.sarvam.ai/text-to-speech';
const SARVAM_TTS_STREAM_URL = 'https://api.sarvam.ai/text-to-speech/stream';
const SARVAM_TTS_WS_URL = 'wss://api.sarvam.ai/text-to-speech/ws';
const SARVAM_STT_WS_URL = 'wss://api.sarvam.ai/speech-to-text/ws';

// --- TTS: REST (full response, for testing/demos) ---

export async function textToSpeech(text, language = 'ta-IN', speaker = 'priya') {
  const res = await fetch(SARVAM_TTS_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'api-subscription-key': SARVAM_API_KEY,
    },
    body: JSON.stringify({
      text,
      target_language_code: language,
      model: 'bulbul:v3',
      speaker,
      speech_sample_rate: '8000',
      output_audio_codec: 'linear16',
      pace: 1.0,
    }),
  });

  if (!res.ok) {
    const err = await res.json();
    throw new Error(`Sarvam TTS error: ${err.error?.message || res.statusText}`);
  }

  const data = await res.json();
  return data.audios[0];
}

// --- TTS: HTTP Streaming (low latency, for production calls) ---

export async function textToSpeechStream(text, language = 'ta-IN', speaker = 'priya', onChunk) {
  const res = await fetch(SARVAM_TTS_STREAM_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'api-subscription-key': SARVAM_API_KEY,
    },
    body: JSON.stringify({
      text,
      target_language_code: language,
      model: 'bulbul:v3',
      speaker,
      output_audio_codec: 'linear16',
      pace: 1.0,
    }),
  });

  if (!res.ok) {
    const err = await res.text();
    throw new Error(`Sarvam TTS stream error: ${err}`);
  }

  const reader = res.body.getReader();
  let firstChunkTime = null;
  const startTime = Date.now();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    if (!firstChunkTime) {
      firstChunkTime = Date.now() - startTime;
      console.log(`[TTS] First audio chunk in ${firstChunkTime}ms`);
    }

    onChunk(Buffer.from(value));
  }

  return { firstChunkMs: firstChunkTime, totalMs: Date.now() - startTime };
}

// --- TTS: WebSocket Streaming (lowest latency, persistent connection) ---

export function createTTSStream(language = 'ta-IN', speaker = 'priya') {
  const ws = new WebSocket(SARVAM_TTS_WS_URL, {
    headers: { 'api-subscription-key': SARVAM_API_KEY },
  });

  let connected = false;
  let onAudioChunk = null;
  let onComplete = null;

  ws.on('open', () => {
    connected = true;
    ws.send(JSON.stringify({
      type: 'config',
      data: {
        model: 'bulbul:v3',
        speaker,
        target_language_code: language,
        pace: 1.0,
        min_buffer_size: 20,
        max_chunk_length: 200,
        output_audio_codec: 'linear16',
      },
    }));
    console.log('[TTS-WS] Connected, config sent');
  });

  ws.on('message', (raw) => {
    try {
      const msg = JSON.parse(raw.toString());
      if (msg.type === 'audio' && msg.data?.audio) {
        const audioBuffer = Buffer.from(msg.data.audio, 'base64');
        if (onAudioChunk) onAudioChunk(audioBuffer);
      } else if (msg.data?.event_type === 'final') {
        if (onComplete) onComplete();
      }
    } catch (e) {
      console.error('[TTS-WS] Parse error:', e.message);
    }
  });

  ws.on('error', (err) => console.error('[TTS-WS] Error:', err.message));
  ws.on('close', () => { connected = false; console.log('[TTS-WS] Closed'); });

  return {
    speak(text, audioCallback, completeCallback) {
      if (!connected) return;
      onAudioChunk = audioCallback;
      onComplete = completeCallback;
      ws.send(JSON.stringify({ type: 'text', data: { text } }));
      ws.send(JSON.stringify({ type: 'flush' }));
    },
    close() {
      if (connected) ws.close();
    },
    isConnected() {
      return connected;
    },
  };
}

// --- STT: WebSocket Streaming ---

export function createSTTStream(language = 'unknown', onTranscript) {
  const ws = new WebSocket(SARVAM_STT_WS_URL, {
    headers: { 'Api-Subscription-Key': SARVAM_API_KEY },
  });

  let connected = false;

  ws.on('open', () => {
    connected = true;
    ws.send(JSON.stringify({
      config: {
        model: 'saaras:v3',
        mode: 'transcribe',
        language_code: language,
        sample_rate: 8000,
        encoding: 'pcm_s16le',
      },
    }));
    console.log('[STT] WebSocket connected, config sent');
  });

  ws.on('message', (raw) => {
    try {
      const msg = JSON.parse(raw.toString());
      if (msg.type === 'data' && msg.data?.transcript) {
        const transcript = msg.data.transcript.trim();
        if (transcript.length > 0) {
          console.log(`[STT] Transcript: "${transcript}"`);
          onTranscript(transcript, msg.data);
        }
      }
    } catch (e) {
      console.error('[STT] Parse error:', e.message);
    }
  });

  ws.on('error', (err) => console.error('[STT] WebSocket error:', err.message));
  ws.on('close', (code, reason) => {
    connected = false;
    console.log(`[STT] WebSocket closed: ${code} ${reason}`);
  });

  return {
    sendAudio(base64PcmChunk) {
      if (!connected) return;
      ws.send(JSON.stringify({
        audio: { data: base64PcmChunk, sample_rate: '8000', encoding: 'pcm_s16le' },
      }));
    },
    close() { if (connected) ws.close(); },
    isConnected() { return connected; },
  };
}
