import { describe, expect, test } from 'bun:test';
import { analyzeAudio, combineChunks, encodeWav16Mono, inspectWav, resampleTo16k } from './voice_eval/static/audio-capture.js';

describe('founder studio audio', () => {
  test('resamples 48 kHz and 44.1 kHz to 16 kHz', () => {
    for (const rate of [48000, 44100]) {
      const input = new Float32Array(rate);
      for (let i = 0; i < input.length; i++) input[i] = Math.sin(2 * Math.PI * 440 * i / rate) * 0.5;
      const output = resampleTo16k(input, rate);
      expect(Math.abs(output.length - 16000)).toBeLessThanOrEqual(1);
      expect(Math.max(...output)).toBeGreaterThan(0.4);
    }
  });

  test('encodes canonical PCM16 mono WAV', () => {
    const samples = new Float32Array([0, 1, -1, 0.5]);
    const wav = encodeWav16Mono(samples);
    expect(inspectWav(wav)).toEqual({format: 1, channels: 1, sampleRate: 16000, bits: 16, dataBytes: 8, samples: 4});
    const view = new DataView(wav);
    expect(view.getInt16(44, true)).toBe(0);
    expect(view.getInt16(46, true)).toBe(32767);
    expect(view.getInt16(48, true)).toBe(-32768);
  });

  test('combines partial chunks without dropping samples', () => {
    expect([...combineChunks([new Float32Array([1, 2]), new Float32Array([3])])]).toEqual([1, 2, 3]);
  });

  test('detects silence and clipping', () => {
    const silence = analyzeAudio(new Float32Array(16000));
    expect(silence.speechRatio).toBe(0);
    const clipped = analyzeAudio(new Float32Array(16000).fill(1));
    expect(clipped.clippedRatio).toBe(1);
  });
});
