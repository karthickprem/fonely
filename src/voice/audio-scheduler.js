import { EventEmitter } from 'events';
import { monotonicNow } from './telemetry.js';

export class AudioScheduler extends EventEmitter {
  constructor(options = {}) {
    super();
    this.sampleRate = options.sampleRate || 24000;
    this.bufferDurationMs = options.bufferDurationMs || 100;
    this.queue = [];
    this.currentGenerationId = 0;
    this.playing = false;
    this.totalSamplesPlayed = 0;
    this.playbackStartTs = null;
    this.lastChunkEndTs = null;
  }

  enqueue(audioBuffer, generationId, metadata = {}) {
    if (generationId < this.currentGenerationId) return;

    this.queue.push({
      audio: audioBuffer,
      generationId,
      metadata,
      enqueuedAt: monotonicNow(),
    });

    if (!this.playing) {
      this._startPlayback();
    }
  }

  _startPlayback() {
    if (this.queue.length === 0) {
      this.playing = false;
      this.emit('idle');
      return;
    }

    this.playing = true;
    const item = this.queue.shift();

    if (item.generationId < this.currentGenerationId) {
      this._startPlayback();
      return;
    }

    if (!this.playbackStartTs) {
      this.playbackStartTs = monotonicNow();
      this.emit('playback_start', {
        generationId: item.generationId,
        timestamp: this.playbackStartTs,
      });
    }

    const samples = item.audio.length / 2;
    const durationMs = (samples / this.sampleRate) * 1000;
    this.totalSamplesPlayed += samples;

    this.emit('chunk_play', {
      audio: item.audio,
      generationId: item.generationId,
      sampleRate: this.sampleRate,
      durationMs,
      timestamp: monotonicNow(),
    });

    this.lastChunkEndTs = monotonicNow() + durationMs;

    setTimeout(() => {
      if (item.generationId >= this.currentGenerationId) {
        this._startPlayback();
      }
    }, durationMs);
  }

  stopImmediate() {
    const now = monotonicNow();
    const wasPlaying = this.playing;
    this.queue = [];
    this.playing = false;

    if (wasPlaying) {
      this.emit('playback_stop', {
        generationId: this.currentGenerationId,
        totalSamplesPlayed: this.totalSamplesPlayed,
        timestamp: now,
      });
    }

    return now;
  }

  cancelGeneration(generationId) {
    this.queue = this.queue.filter((item) => item.generationId > generationId);
    if (this.currentGenerationId <= generationId) {
      const stopTs = this.stopImmediate();
      this.currentGenerationId = generationId + 1;
      return stopTs;
    }
    return null;
  }

  setGeneration(generationId) {
    this.currentGenerationId = generationId;
    this.totalSamplesPlayed = 0;
    this.playbackStartTs = null;
  }

  isPlaying() {
    return this.playing;
  }

  queueDepth() {
    return this.queue.length;
  }

  getPlayedDurationMs() {
    return (this.totalSamplesPlayed / this.sampleRate) * 1000;
  }
}
