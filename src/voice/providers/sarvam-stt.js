import { WebSocket } from 'ws';
import { EventEmitter } from 'events';
import { monotonicNow } from '../telemetry.js';

const SARVAM_STT_WS_URL = 'wss://api.sarvam.ai/speech-to-text/ws';
const READY_TIMEOUT_MS = 10000;
const MAX_PENDING_FRAMES = 50;

export class SarvamSTTStream extends EventEmitter {
  constructor(options = {}) {
    super();
    this.apiKey = options.apiKey || process.env.SARVAM_API_KEY;
    this.sampleRate = options.sampleRate || 16000;
    this.language = options.language || 'unknown';
    this.sessionId = options.sessionId || crypto.randomUUID();
    this.turnId = 0;
    this.generationId = 0;
    this.ws = null;
    this.connected = false;
    this.connecting = false;
    this.closed = false;
    this.pendingFrames = [];
    this.readyTimeout = null;
  }

  connect() {
    if (this.closed || this.connecting || this.connected) return;
    this.connecting = true;

    console.log('[STT] Connecting to Sarvam...');

    this.ws = new WebSocket(SARVAM_STT_WS_URL, {
      headers: { 'Api-Subscription-Key': this.apiKey },
    });

    this.readyTimeout = setTimeout(() => {
      if (!this.connected) {
        console.log('[STT] Connection timeout');
        this.connecting = false;
        this.emit('error', new Error('STT connection timeout'));
        this.ws?.close();
      }
    }, READY_TIMEOUT_MS);

    this.ws.on('open', () => {
      this.connected = true;
      this.connecting = false;
      clearTimeout(this.readyTimeout);

      this.ws.send(JSON.stringify({
        config: {
          model: 'saaras:v3',
          mode: 'transcribe',
          language_code: this.language,
          sample_rate: this.sampleRate,
          encoding: 'pcm_s16le',
        },
      }));

      console.log('[STT] Connected and configured');
      this.emit('ready');

      this._drainPendingFrames();
    });

    this.ws.on('message', (raw) => {
      try {
        const msg = JSON.parse(raw.toString());
        this._handleMessage(msg);
      } catch (e) {
        this.emit('error', new Error('STT parse error: ' + e.message));
      }
    });

    this.ws.on('error', (err) => {
      console.error('[STT] WebSocket error:', err.message);
      this.emit('error', err);
    });

    this.ws.on('close', (code, reason) => {
      console.log(`[STT] Closed: ${code} ${reason?.toString() || ''}`);
      this.connected = false;
      this.connecting = false;
      clearTimeout(this.readyTimeout);
      this.emit('disconnected', { code, reason: reason?.toString() });
    });
  }

  _handleMessage(msg) {
    const now = monotonicNow();

    if (msg.type === 'data' && msg.data) {
      const transcript = (msg.data.transcript || '').trim();
      if (!transcript) return;

      const isFinal = msg.data.is_final !== false;
      const detectedLanguage = msg.data.language_code || null;

      console.log(`[STT] ${isFinal ? 'FINAL' : 'partial'}: "${transcript}" [${detectedLanguage}]`);

      const event = {
        type: isFinal ? 'final' : 'partial',
        transcript,
        language: detectedLanguage,
        sessionId: this.sessionId,
        turnId: this.turnId,
        generationId: this.generationId,
        timestamp: now,
      };

      this.emit('transcript', event);

      if (isFinal) {
        this.emit('final', event);
      } else {
        this.emit('partial', event);
      }
    }
  }

  sendAudio(pcmBuffer) {
    const base64 = typeof pcmBuffer === 'string'
      ? pcmBuffer
      : Buffer.from(pcmBuffer).toString('base64');

    if (this.connected && this.ws) {
      this.ws.send(JSON.stringify({
        audio: {
          data: base64,
          sample_rate: String(this.sampleRate),
          encoding: 'pcm_s16le',
        },
      }));
      return;
    }

    if (this.pendingFrames.length < MAX_PENDING_FRAMES) {
      this.pendingFrames.push(base64);
    }

    if (!this.connecting && !this.closed) {
      this.connect();
    }
  }

  _drainPendingFrames() {
    while (this.pendingFrames.length > 0 && this.connected) {
      const frame = this.pendingFrames.shift();
      this.ws.send(JSON.stringify({
        audio: {
          data: frame,
          sample_rate: String(this.sampleRate),
          encoding: 'pcm_s16le',
        },
      }));
    }
  }

  flush() {
    if (!this.connected || !this.ws) return;
    this.ws.send(JSON.stringify({ type: 'flush' }));
  }

  newTurn() {
    this.turnId++;
    return this.turnId;
  }

  newGeneration() {
    this.generationId++;
    return this.generationId;
  }

  close() {
    this.closed = true;
    clearTimeout(this.readyTimeout);
    this.pendingFrames = [];
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.connected = false;
    this.connecting = false;
  }

  isConnected() {
    return this.connected;
  }
}
