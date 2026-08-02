import { WebSocket } from 'ws';
import { EventEmitter } from 'events';
import { monotonicNow } from '../telemetry.js';

const SARVAM_STT_WS_URL = 'wss://api.sarvam.ai/speech-to-text/ws';
const RECONNECT_DELAY_MS = 2000;
const MAX_RECONNECTS = 5;
const READY_TIMEOUT_MS = 10000;
const IDLE_TIMEOUT_MS = 60000;

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
    this.reconnectCount = 0;
    this.readyTimeout = null;
    this.idleTimeout = null;
    this.closed = false;
  }

  connect() {
    if (this.closed) return;
    this._clearTimeouts();

    this.ws = new WebSocket(SARVAM_STT_WS_URL, {
      headers: { 'Api-Subscription-Key': this.apiKey },
    });

    this.readyTimeout = setTimeout(() => {
      if (!this.connected) {
        this.emit('error', new Error('STT connection timeout'));
        this.ws?.close();
      }
    }, READY_TIMEOUT_MS);

    this.ws.on('open', () => {
      this.connected = true;
      this.reconnectCount = 0;
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

      this.emit('ready');
      this._resetIdleTimeout();
    });

    this.ws.on('message', (raw) => {
      this._resetIdleTimeout();
      try {
        const msg = JSON.parse(raw.toString());
        this._handleMessage(msg);
      } catch (e) {
        this.emit('error', new Error('STT parse error: ' + e.message));
      }
    });

    this.ws.on('error', (err) => {
      this.emit('error', err);
    });

    this.ws.on('close', (code, reason) => {
      this.connected = false;
      this._clearTimeouts();
      this.emit('disconnected', { code, reason: reason?.toString() });

      if (!this.closed && this.reconnectCount < MAX_RECONNECTS) {
        this.reconnectCount++;
        setTimeout(() => this.connect(), RECONNECT_DELAY_MS * this.reconnectCount);
      }
    });
  }

  _handleMessage(msg) {
    const now = monotonicNow();

    if (msg.type === 'data' && msg.data) {
      const transcript = (msg.data.transcript || '').trim();
      if (!transcript) return;

      const isFinal = msg.data.is_final !== false;
      const detectedLanguage = msg.data.language_code || null;

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
    if (!this.connected || !this.ws) return;
    this._resetIdleTimeout();

    const base64 = typeof pcmBuffer === 'string'
      ? pcmBuffer
      : Buffer.from(pcmBuffer).toString('base64');

    this.ws.send(JSON.stringify({
      audio: {
        data: base64,
        sample_rate: String(this.sampleRate),
        encoding: 'pcm_s16le',
      },
    }));
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

  _resetIdleTimeout() {
    clearTimeout(this.idleTimeout);
    this.idleTimeout = setTimeout(() => {
      this.emit('idle');
    }, IDLE_TIMEOUT_MS);
  }

  _clearTimeouts() {
    clearTimeout(this.readyTimeout);
    clearTimeout(this.idleTimeout);
  }

  close() {
    this.closed = true;
    this._clearTimeouts();
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
