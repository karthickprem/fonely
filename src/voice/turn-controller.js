import { EventEmitter } from 'events';
import { monotonicNow } from './telemetry.js';

const SILENCE_TIMEOUT_MS = 3000;
const ENDPOINTING_DELAY_MS = 800;
const MIN_SPEECH_DURATION_MS = 300;
const INTERRUPTION_COOLDOWN_MS = 500;

export class TurnController extends EventEmitter {
  constructor(options = {}) {
    super();
    this.silenceTimeoutMs = options.silenceTimeoutMs || SILENCE_TIMEOUT_MS;
    this.endpointingDelayMs = options.endpointingDelayMs || ENDPOINTING_DELAY_MS;
    this.minSpeechDurationMs = options.minSpeechDurationMs || MIN_SPEECH_DURATION_MS;

    this.state = 'idle';
    this.generationId = 0;
    this.turnId = 0;

    this.speechStartTs = null;
    this.lastSpeechTs = null;
    this.agentSpeaking = false;
    this.lastInterruptionTs = null;

    this.silenceTimer = null;
    this.endpointTimer = null;
  }

  onVoiceActivity(active) {
    const now = monotonicNow();

    if (active) {
      this._clearEndpointTimer();

      if (this.agentSpeaking) {
        if (this.lastInterruptionTs &&
            (now - this.lastInterruptionTs) < INTERRUPTION_COOLDOWN_MS) {
          return;
        }

        if (!this.speechStartTs) {
          this.speechStartTs = now;
          return;
        }

        const speechDuration = now - this.speechStartTs;
        if (speechDuration < this.minSpeechDurationMs) {
          return;
        }

        this.lastInterruptionTs = now;
        this.generationId++;
        this.emit('interruption', {
          generationId: this.generationId,
          turnId: this.turnId,
          timestamp: now,
        });
        this.agentSpeaking = false;
      }

      if (this.state !== 'listening') {
        this.state = 'listening';
        if (!this.speechStartTs) this.speechStartTs = now;
        this.emit('listening_start', { turnId: this.turnId, timestamp: now });
      }

      this.lastSpeechTs = now;
      this._resetSilenceTimer();
    } else {
      if (this.state === 'listening' && this.lastSpeechTs) {
        this._startEndpointTimer();
      }
    }
  }

  onFinalTranscript(transcript) {
    const now = monotonicNow();
    this._clearEndpointTimer();
    this._clearSilenceTimer();

    if (this.state === 'listening' || this.state === 'idle') {
      this.turnId++;
      this.state = 'processing';
      this.emit('turn_end', {
        turnId: this.turnId,
        generationId: this.generationId,
        transcript,
        micEndTs: this.lastSpeechTs || now,
        sttFinalTs: now,
      });
      this.speechStartTs = null;
      this.lastSpeechTs = null;
    }
  }

  onAgentSpeakStart(generationId) {
    this.agentSpeaking = true;
    this.state = 'agent_speaking';
    this.speechStartTs = null;
    this.emit('agent_speak_start', { generationId, timestamp: monotonicNow() });
  }

  onAgentSpeakEnd(generationId) {
    if (this.agentSpeaking && generationId >= this.generationId) {
      this.agentSpeaking = false;
      this.state = 'idle';
      this.emit('agent_speak_end', { generationId, timestamp: monotonicNow() });
      this._resetSilenceTimer();
    }
  }

  onFalseInterruption() {
    this.emit('false_interruption', {
      generationId: this.generationId,
      turnId: this.turnId,
      timestamp: monotonicNow(),
    });
  }

  _startEndpointTimer() {
    this._clearEndpointTimer();
    this.endpointTimer = setTimeout(() => {
      if (this.state === 'listening') {
        this.emit('endpoint', {
          turnId: this.turnId,
          timestamp: monotonicNow(),
        });
      }
    }, this.endpointingDelayMs);
  }

  _resetSilenceTimer() {
    this._clearSilenceTimer();
    this.silenceTimer = setTimeout(() => {
      if (this.state === 'idle' || this.state === 'listening') {
        this.emit('silence_timeout', {
          turnId: this.turnId,
          timestamp: monotonicNow(),
          durationMs: this.silenceTimeoutMs,
        });
      }
    }, this.silenceTimeoutMs);
  }

  _clearSilenceTimer() {
    clearTimeout(this.silenceTimer);
    this.silenceTimer = null;
  }

  _clearEndpointTimer() {
    clearTimeout(this.endpointTimer);
    this.endpointTimer = null;
  }

  getState() {
    return {
      state: this.state,
      generationId: this.generationId,
      turnId: this.turnId,
      agentSpeaking: this.agentSpeaking,
    };
  }

  reset() {
    this._clearSilenceTimer();
    this._clearEndpointTimer();
    this.state = 'idle';
    this.speechStartTs = null;
    this.lastSpeechTs = null;
    this.agentSpeaking = false;
    this.lastInterruptionTs = null;
  }

  destroy() {
    this.reset();
    this.removeAllListeners();
  }
}
