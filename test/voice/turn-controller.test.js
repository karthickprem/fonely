import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import { TurnController } from '../../src/voice/turn-controller.js';

describe('TurnController', () => {
  let tc;

  beforeEach(() => {
    tc = new TurnController({
      silenceTimeoutMs: 500,
      endpointingDelayMs: 200,
      minSpeechDurationMs: 100,
    });
  });

  afterEach(() => {
    tc.destroy();
  });

  it('starts in idle state', () => {
    const state = tc.getState();
    expect(state.state).toBe('idle');
    expect(state.generationId).toBe(0);
    expect(state.turnId).toBe(0);
  });

  it('transitions to listening on voice activity', () => {
    tc.onVoiceActivity(true);
    expect(tc.getState().state).toBe('listening');
  });

  it('emits turn_end on final transcript', () => {
    return new Promise((resolve) => {
      tc.on('turn_end', (event) => {
        expect(event.turnId).toBe(1);
        expect(event.transcript).toBe('Hello');
        resolve();
      });

      tc.onVoiceActivity(true);
      tc.onFinalTranscript('Hello');
    });
  });

  it('increments turn ID on each turn', () => {
    return new Promise((resolve) => {
      let turnCount = 0;
      tc.on('turn_end', (event) => {
        turnCount++;
        if (turnCount === 1) {
          expect(event.turnId).toBe(1);
          tc.state = 'idle';
          tc.onVoiceActivity(true);
          tc.onFinalTranscript('Second turn');
        } else if (turnCount === 2) {
          expect(event.turnId).toBe(2);
          resolve();
        }
      });

      tc.onVoiceActivity(true);
      tc.onFinalTranscript('First turn');
    });
  });

  it('transitions through agent speaking states', () => {
    tc.onAgentSpeakStart(1);
    expect(tc.getState().state).toBe('agent_speaking');
    expect(tc.getState().agentSpeaking).toBe(true);

    tc.onAgentSpeakEnd(1);
    expect(tc.getState().state).toBe('idle');
    expect(tc.getState().agentSpeaking).toBe(false);
  });

  it('emits silence_timeout', () => {
    return new Promise((resolve) => {
      tc.on('silence_timeout', (event) => {
        expect(event.durationMs).toBeGreaterThan(0);
        resolve();
      });

      tc.onAgentSpeakStart(0);
      tc.onAgentSpeakEnd(0);
    });
  });

  it('resets cleanly', () => {
    tc.onVoiceActivity(true);
    tc.onAgentSpeakStart(1);
    tc.reset();

    const state = tc.getState();
    expect(state.state).toBe('idle');
    expect(state.agentSpeaking).toBe(false);
  });
});
