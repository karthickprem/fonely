import { describe, it, expect } from 'bun:test';
import {
  createTurnMetrics,
  deriveTurnLatency,
  percentile,
  SessionTelemetry,
} from '../../src/voice/telemetry.js';

describe('createTurnMetrics', () => {
  it('creates metrics with correct IDs', () => {
    const m = createTurnMetrics(5, 3);
    expect(m.turnId).toBe(5);
    expect(m.generationId).toBe(3);
    expect(m.interrupted).toBe(false);
    expect(m.falseInterruption).toBe(false);
    expect(m.micEndTs).toBeNull();
  });
});

describe('deriveTurnLatency', () => {
  it('computes STT latency', () => {
    const m = createTurnMetrics(1, 1);
    m.micEndTs = 100;
    m.sttFinalTs = 350;
    const latency = deriveTurnLatency(m);
    expect(latency.sttLatencyMs).toBe(250);
  });

  it('computes end-to-end latency', () => {
    const m = createTurnMetrics(1, 1);
    m.micEndTs = 100;
    m.sttFinalTs = 300;
    m.llmStartTs = 300;
    m.llmEndTs = 600;
    m.ttsFirstAudioTs = 700;
    m.playbackStartTs = 750;
    const latency = deriveTurnLatency(m);
    expect(latency.endToEndMs).toBe(650);
    expect(latency.llmLatencyMs).toBe(300);
    expect(latency.ttsFirstAudioMs).toBe(100);
  });

  it('returns null for missing timestamps', () => {
    const m = createTurnMetrics(1, 1);
    const latency = deriveTurnLatency(m);
    expect(latency.sttLatencyMs).toBeNull();
    expect(latency.endToEndMs).toBeNull();
  });

  it('computes interruption-to-stop latency', () => {
    const m = createTurnMetrics(1, 1);
    m.interruptionTs = 500;
    m.audioStopTs = 700;
    m.interrupted = true;
    const latency = deriveTurnLatency(m);
    expect(latency.interruptionToStopMs).toBe(200);
    expect(latency.interrupted).toBe(true);
  });
});

describe('percentile', () => {
  it('computes p50', () => {
    expect(percentile([1, 2, 3, 4, 5], 50)).toBe(3);
  });

  it('computes p95 of small set', () => {
    expect(percentile([10, 20, 30], 95)).toBe(30);
  });

  it('returns null for empty array', () => {
    expect(percentile([], 50)).toBeNull();
  });

  it('filters non-finite values', () => {
    expect(percentile([1, null, 3, undefined, 5], 50)).toBe(3);
  });
});

describe('SessionTelemetry', () => {
  it('tracks turns and aggregates', () => {
    const session = new SessionTelemetry('test-session');

    const m1 = createTurnMetrics(1, 1);
    m1.micEndTs = 100;
    m1.playbackStartTs = 900;
    m1.sttLanguage = 'ta-IN';
    session.recordTurn(m1);

    const m2 = createTurnMetrics(2, 2);
    m2.micEndTs = 2000;
    m2.playbackStartTs = 3200;
    m2.sttLanguage = 'en-IN';
    session.recordTurn(m2);

    const agg = session.aggregate();
    expect(agg.totalTurns).toBe(2);
    expect(agg.endToEnd.p50Ms).not.toBeNull();
    expect(agg.sttLanguages.sort()).toEqual(['en-IN', 'ta-IN']);
  });

  it('exports sanitized metrics without transcripts', () => {
    const session = new SessionTelemetry('test-session');
    const m = createTurnMetrics(1, 1);
    m.micEndTs = 100;
    m.playbackStartTs = 800;
    session.recordTurn(m);

    const exported = session.exportSanitized();
    expect(exported.aggregate).toBeTruthy();
    expect(exported.turns.length).toBe(1);
    expect(exported.turns[0]).not.toHaveProperty('transcript');
  });

  it('computes false interruption rate', () => {
    const session = new SessionTelemetry('test-session');

    const m1 = createTurnMetrics(1, 1);
    m1.interrupted = true;
    m1.falseInterruption = false;
    m1.interruptionTs = 100;
    m1.audioStopTs = 300;
    session.recordTurn(m1);

    const m2 = createTurnMetrics(2, 2);
    m2.interrupted = true;
    m2.falseInterruption = true;
    m2.interruptionTs = 500;
    m2.audioStopTs = 700;
    session.recordTurn(m2);

    const agg = session.aggregate();
    expect(agg.interruptionCount).toBe(2);
    expect(agg.falseInterruptionCount).toBe(1);
    expect(agg.falseInterruptionRate).toBe(0.5);
  });
});
