import { describe, it, expect } from 'bun:test';
import { AudioScheduler } from '../../src/voice/audio-scheduler.js';

describe('AudioScheduler', () => {
  it('starts not playing', () => {
    const scheduler = new AudioScheduler();
    expect(scheduler.isPlaying()).toBe(false);
    expect(scheduler.queueDepth()).toBe(0);
  });

  it('enqueues audio and emits chunk_play', () => {
    return new Promise((resolve) => {
      const scheduler = new AudioScheduler({ sampleRate: 8000 });
      const pcm = Buffer.alloc(1600);

      scheduler.on('chunk_play', (event) => {
        expect(event.sampleRate).toBe(8000);
        expect(event.durationMs).toBeGreaterThan(0);
        resolve();
      });

      scheduler.enqueue(pcm, 0);
    });
  });

  it('discards audio from stale generations', () => {
    const scheduler = new AudioScheduler();
    scheduler.setGeneration(5);

    const initialDepth = scheduler.queueDepth();
    scheduler.enqueue(Buffer.alloc(100), 3);
    expect(scheduler.queueDepth()).toBe(initialDepth);
  });

  it('cancelGeneration increments current generation', () => {
    const scheduler = new AudioScheduler();
    scheduler.cancelGeneration(1);
    expect(scheduler.currentGenerationId).toBeGreaterThanOrEqual(2);
  });

  it('stops immediately and emits playback_stop', () => {
    return new Promise((resolve) => {
      const scheduler = new AudioScheduler({ sampleRate: 8000 });

      scheduler.on('playback_stop', () => {
        expect(scheduler.isPlaying()).toBe(false);
        resolve();
      });

      scheduler.on('chunk_play', () => {
        scheduler.stopImmediate();
      });

      scheduler.enqueue(Buffer.alloc(16000), 0);
    });
  });

  it('tracks played duration starting at zero', () => {
    const scheduler = new AudioScheduler({ sampleRate: 16000 });
    expect(scheduler.getPlayedDurationMs()).toBe(0);
  });

  it('setGeneration resets state', () => {
    const scheduler = new AudioScheduler();
    scheduler.setGeneration(10);
    expect(scheduler.currentGenerationId).toBe(10);
    expect(scheduler.getPlayedDurationMs()).toBe(0);
  });
});
