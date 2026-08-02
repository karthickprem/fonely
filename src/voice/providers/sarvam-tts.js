import { WebSocket } from 'ws';
import { EventEmitter } from 'events';
import { monotonicNow } from '../telemetry.js';

const SARVAM_TTS_WS_URL = 'wss://api.sarvam.ai/text-to-speech/ws';
const SARVAM_TTS_REST_URL = 'https://api.sarvam.ai/text-to-speech';
const READY_TIMEOUT_MS = 10000;
const RECONNECT_DELAY_MS = 2000;
const MAX_RECONNECTS = 3;

export class SarvamTTSStream extends EventEmitter {
  constructor(options = {}) {
    super();
    this.apiKey = options.apiKey || process.env.SARVAM_API_KEY;
    this.language = options.language || 'ta-IN';
    this.speaker = options.speaker || 'priya';
    this.pace = options.pace || 1.0;
    this.sampleRate = options.sampleRate || 24000;
    this.ws = null;
    this.connected = false;
    this.reconnectCount = 0;
    this.closed = false;
    this.currentGenerationId = 0;
    this.pendingChunks = [];
    this.speaking = false;
    this.firstAudioTs = null;
    this.speakStartTs = null;
  }

  connect() {
    if (this.closed || this.connected || this._connecting) return;
    this._connecting = true;

    this.ws = new WebSocket(SARVAM_TTS_WS_URL, {
      headers: { 'api-subscription-key': this.apiKey },
    });

    const readyTimeout = setTimeout(() => {
      if (!this.connected) {
        this.emit('error', new Error('TTS connection timeout'));
        this.ws?.close();
      }
    }, READY_TIMEOUT_MS);

    this.ws.on('open', () => {
      this.connected = true;
      this._connecting = false;
      this.reconnectCount = 0;
      clearTimeout(readyTimeout);

      this.ws.send(JSON.stringify({
        type: 'config',
        data: {
          model: 'bulbul:v3',
          speaker: this.speaker,
          target_language_code: this.language,
          pace: this.pace,
          min_buffer_size: 20,
          max_chunk_length: 200,
          output_audio_codec: 'linear16',
          speech_sample_rate: String(this.sampleRate),
        },
      }));

      console.log('[TTS] Connected and configured');
      this.emit('ready');
      this._processQueue();
    });

    this.ws.on('message', (raw) => {
      try {
        const msg = JSON.parse(raw.toString());
        this._handleMessage(msg);
      } catch (e) {
        this.emit('error', new Error('TTS parse error: ' + e.message));
      }
    });

    this.ws.on('error', (err) => {
      this.emit('error', err);
    });

    this.ws.on('close', (code) => {
      this.connected = false;
      this._connecting = false;
      clearTimeout(readyTimeout);
      console.log(`[TTS] Closed: ${code}`);
      this.emit('disconnected', { code });
    });
  }

  _handleMessage(msg) {
    const now = monotonicNow();

    if (msg.type === 'audio' && msg.data?.audio) {
      if (!this.firstAudioTs) {
        this.firstAudioTs = now;
        this.emit('first_audio', {
          generationId: this.currentGenerationId,
          latencyMs: this.speakStartTs ? now - this.speakStartTs : null,
          timestamp: now,
        });
      }

      const audioBuffer = Buffer.from(msg.data.audio, 'base64');
      this.emit('audio_chunk', {
        generationId: this.currentGenerationId,
        audio: audioBuffer,
        sampleRate: this.sampleRate,
        timestamp: now,
      });
    }

    if (msg.data?.event_type === 'final') {
      this.speaking = false;
      this.emit('synthesis_complete', {
        generationId: this.currentGenerationId,
        firstAudioMs: this.firstAudioTs && this.speakStartTs
          ? this.firstAudioTs - this.speakStartTs : null,
        totalMs: this.speakStartTs ? now - this.speakStartTs : null,
        timestamp: now,
      });
      this._processQueue();
    }
  }

  speak(text, generationId) {
    if (generationId !== undefined) {
      this.currentGenerationId = generationId;
    }

    if (!this.connected || this.speaking) {
      this.pendingChunks.push({ text, generationId: this.currentGenerationId });
      if (!this.connected && !this.closed) {
        this.connect();
      }
      return;
    }

    this._sendText(text);
  }

  _sendText(text) {
    this.speaking = true;
    this.firstAudioTs = null;
    this.speakStartTs = monotonicNow();

    this.ws.send(JSON.stringify({ type: 'text', data: { text } }));
    this.ws.send(JSON.stringify({ type: 'flush' }));
  }

  _processQueue() {
    if (this.pendingChunks.length === 0) return;

    const staleId = this.currentGenerationId;
    const nextChunk = this.pendingChunks[0];

    if (nextChunk.generationId < staleId) {
      this.pendingChunks.shift();
      this._processQueue();
      return;
    }

    this.pendingChunks.shift();
    this.currentGenerationId = nextChunk.generationId;
    this._sendText(nextChunk.text);
  }

  cancelGeneration(generationId) {
    this.pendingChunks = this.pendingChunks.filter(
      (c) => c.generationId > generationId
    );
    if (this.currentGenerationId <= generationId) {
      this.speaking = false;
      this.currentGenerationId = generationId + 1;
    }
    this.emit('cancelled', { generationId, timestamp: monotonicNow() });
  }

  stop() {
    this.pendingChunks = [];
    this.speaking = false;
    this.emit('stopped', { timestamp: monotonicNow() });
  }

  updateVoice(options) {
    if (options.speaker) this.speaker = options.speaker;
    if (options.language) this.language = options.language;
    if (options.pace) this.pace = options.pace;

    if (this.connected) {
      this.ws.send(JSON.stringify({
        type: 'config',
        data: {
          model: 'bulbul:v3',
          speaker: this.speaker,
          target_language_code: this.language,
          pace: this.pace,
          min_buffer_size: 20,
          max_chunk_length: 200,
          output_audio_codec: 'linear16',
          speech_sample_rate: String(this.sampleRate),
        },
      }));
    }
  }

  close() {
    this.closed = true;
    this.pendingChunks = [];
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.connected = false;
  }

  isConnected() {
    return this.connected;
  }
}

export async function restTTS(text, options = {}) {
  const apiKey = options.apiKey || process.env.SARVAM_API_KEY;
  const language = options.language || 'ta-IN';
  const speaker = options.speaker || 'priya';
  const pace = options.pace || 1.0;
  const sampleRate = options.sampleRate || 24000;

  const res = await fetch(SARVAM_TTS_REST_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'api-subscription-key': apiKey,
    },
    body: JSON.stringify({
      text,
      target_language_code: language,
      model: 'bulbul:v3',
      speaker,
      speech_sample_rate: String(sampleRate),
      output_audio_codec: 'linear16',
      pace,
    }),
  });

  if (!res.ok) {
    const err = await res.text();
    throw new Error('Sarvam TTS REST error: ' + err);
  }

  const data = await res.json();
  return {
    audio: Buffer.from(data.audios[0], 'base64'),
    sampleRate,
  };
}
