import { performance } from 'node:perf_hooks';

export function monotonicNow() {
  return performance.now();
}

export function createTurnMetrics(turnId, generationId) {
  return {
    turnId,
    generationId,
    micEndTs: null,
    sttFinalTs: null,
    llmStartTs: null,
    llmEndTs: null,
    ttsFirstAudioTs: null,
    playbackStartTs: null,
    playbackEndTs: null,
    interruptionTs: null,
    audioStopTs: null,
    sttLanguage: null,
    interrupted: false,
    falseInterruption: false,
  };
}

export function deriveTurnLatency(m) {
  const diff = (a, b) =>
    Number.isFinite(m[a]) && Number.isFinite(m[b]) ? Math.max(0, m[a] - m[b]) : null;

  return {
    turnId: m.turnId,
    generationId: m.generationId,
    sttLatencyMs: diff('sttFinalTs', 'micEndTs'),
    llmLatencyMs: diff('llmEndTs', 'llmStartTs'),
    ttsFirstAudioMs: diff('ttsFirstAudioTs', 'llmEndTs'),
    endToEndMs: diff('playbackStartTs', 'micEndTs'),
    interruptionToStopMs: diff('audioStopTs', 'interruptionTs'),
    sttLanguage: m.sttLanguage,
    interrupted: m.interrupted,
    falseInterruption: m.falseInterruption,
  };
}

export function percentile(values, pct) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (sorted.length === 0) return null;
  const idx = Math.ceil((pct / 100) * sorted.length) - 1;
  return sorted[Math.max(0, Math.min(idx, sorted.length - 1))];
}

export class SessionTelemetry {
  constructor(sessionId) {
    this.sessionId = sessionId;
    this.turns = [];
    this.startedAt = monotonicNow();
  }

  recordTurn(metrics) {
    const latency = deriveTurnLatency(metrics);
    this.turns.push(latency);
    return latency;
  }

  aggregate() {
    const e2e = this.turns.map((t) => t.endToEndMs).filter(Number.isFinite);
    const intStop = this.turns
      .filter((t) => t.interrupted && !t.falseInterruption)
      .map((t) => t.interruptionToStopMs)
      .filter(Number.isFinite);
    const totalTurns = this.turns.length;
    const interrupted = this.turns.filter((t) => t.interrupted).length;
    const falseInterruptions = this.turns.filter((t) => t.falseInterruption).length;

    return {
      sessionId: this.sessionId,
      totalTurns,
      endToEnd: {
        p50Ms: percentile(e2e, 50),
        p95Ms: percentile(e2e, 95),
      },
      interruptionToStop: {
        p50Ms: percentile(intStop, 50),
        p95Ms: percentile(intStop, 95),
      },
      interruptionCount: interrupted,
      falseInterruptionCount: falseInterruptions,
      falseInterruptionRate:
        interrupted > 0 ? falseInterruptions / interrupted : 0,
      sttLanguages: [...new Set(this.turns.map((t) => t.sttLanguage).filter(Boolean))],
      durationMs: monotonicNow() - this.startedAt,
    };
  }

  exportSanitized() {
    return {
      aggregate: this.aggregate(),
      turns: this.turns.map((t) => ({
        turnId: t.turnId,
        endToEndMs: t.endToEndMs,
        sttLatencyMs: t.sttLatencyMs,
        llmLatencyMs: t.llmLatencyMs,
        ttsFirstAudioMs: t.ttsFirstAudioMs,
        interruptionToStopMs: t.interruptionToStopMs,
        sttLanguage: t.sttLanguage,
        interrupted: t.interrupted,
      })),
    };
  }
}
