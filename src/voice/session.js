import { SarvamSTTStream } from './providers/sarvam-stt.js';
import { SarvamTTSStream, restTTS } from './providers/sarvam-tts.js';
import { AudioScheduler } from './audio-scheduler.js';
import { TurnController } from './turn-controller.js';
import { createSpeakablePlan } from './speakable-plan.js';
import { chatWithLLM, checkSafetyRules, getGreeting } from './dental-demo.js';
import { createTurnMetrics, monotonicNow, SessionTelemetry } from './telemetry.js';

const MAX_MESSAGE_BYTES = 64 * 1024;

export function handleVoiceLabSession(ws) {
  const sessionId = crypto.randomUUID();
  const telemetry = new SessionTelemetry(sessionId);
  const messages = [];
  let turnMetrics = null;
  let turnId = 0;
  let generationId = 0;
  let currentSpeaker = 'priya';
  let currentLanguage = 'ta-IN';
  let processing = false;

  const stt = new SarvamSTTStream({ sessionId, sampleRate: 16000 });
  const tts = new SarvamTTSStream({ speaker: currentSpeaker, language: currentLanguage });
  const scheduler = new AudioScheduler({ sampleRate: 24000 });
  const turnController = new TurnController();

  const send = (event) => {
    if (ws.readyState === ws.OPEN || ws.readyState === 1) {
      ws.send(JSON.stringify(event));
    }
  };

  send({ type: 'session_start', sessionId });

  stt.on('ready', () => send({ type: 'stt_ready' }));
  tts.on('ready', () => send({ type: 'tts_ready' }));

  stt.on('partial', (event) => {
    send({
      type: 'stt_partial',
      transcript: event.transcript,
      language: event.language,
      turnId: event.turnId,
    });
  });

  stt.on('final', (event) => {
    if (turnMetrics) {
      turnMetrics.sttFinalTs = event.timestamp;
      turnMetrics.sttLanguage = event.language;
    }
    send({
      type: 'stt_final',
      transcript: event.transcript,
      language: event.language,
      turnId: event.turnId,
    });
    turnController.onFinalTranscript(event.transcript);
  });

  stt.on('error', (err) => {
    send({ type: 'stt_error', message: err.message });
  });

  tts.on('audio_chunk', (event) => {
    if (event.generationId < generationId) return;
    send({
      type: 'audio_chunk',
      audio: event.audio.toString('base64'),
      sampleRate: event.sampleRate,
      generationId: event.generationId,
    });
  });

  tts.on('first_audio', (event) => {
    if (turnMetrics) turnMetrics.ttsFirstAudioTs = event.timestamp;
    send({
      type: 'tts_first_audio',
      generationId: event.generationId,
      latencyMs: event.latencyMs,
    });
  });

  tts.on('synthesis_complete', (event) => {
    send({
      type: 'tts_complete',
      generationId: event.generationId,
      totalMs: event.totalMs,
    });
  });

  tts.on('error', (err) => {
    send({ type: 'tts_error', message: err.message });
  });

  turnController.on('turn_end', async (event) => {
    if (processing) return;
    processing = true;

    turnId = event.turnId;
    if (turnMetrics) {
      turnMetrics.micEndTs = event.micEndTs;
      turnMetrics.sttFinalTs = event.sttFinalTs;
    }

    try {
      await processUserTurn(event.transcript);
    } finally {
      processing = false;
    }
  });

  turnController.on('interruption', (event) => {
    generationId = event.generationId;
    tts.cancelGeneration(event.generationId - 1);
    scheduler.cancelGeneration(event.generationId - 1);

    if (turnMetrics) {
      turnMetrics.interruptionTs = event.timestamp;
      turnMetrics.interrupted = true;
      turnMetrics.audioStopTs = monotonicNow();
    }

    send({
      type: 'interruption',
      generationId: event.generationId,
      timestamp: event.timestamp,
    });
  });

  turnController.on('false_interruption', (event) => {
    if (turnMetrics) turnMetrics.falseInterruption = true;
    send({ type: 'false_interruption', generationId: event.generationId });
  });

  turnController.on('silence_timeout', () => {
    send({ type: 'silence_prompt' });
  });

  async function processUserTurn(transcript) {
    generationId++;
    turnMetrics = createTurnMetrics(turnId, generationId);
    turnMetrics.micEndTs = monotonicNow();

    const safety = checkSafetyRules(transcript);
    let responseText;

    if (!safety.safe) {
      const lang = currentLanguage.startsWith('ta') ? 'ta' : 'en';
      responseText = lang === 'ta' ? safety.responseTa : safety.response;
      send({ type: 'safety_triggered', safetyType: safety.type });
    } else {
      messages.push({ role: 'user', content: transcript });
      turnMetrics.llmStartTs = monotonicNow();
      responseText = await chatWithLLM(messages);
      turnMetrics.llmEndTs = monotonicNow();
      messages.push({ role: 'assistant', content: responseText });
    }

    send({
      type: 'transcript',
      role: 'assistant',
      text: responseText,
      turnId,
      generationId,
    });

    const plan = createSpeakablePlan(responseText, {
      lang: currentLanguage.startsWith('ta') ? 'ta' : 'en',
      turnIndex: turnId,
    });

    send({
      type: 'speakable_plan',
      plan: { ...plan, generationId },
    });

    turnController.onAgentSpeakStart(generationId);

    for (const chunk of plan.chunks) {
      if (generationId > turnMetrics.generationId) break;

      try {
        tts.speak(chunk.text, generationId);
      } catch {
        try {
          const result = await restTTS(chunk.text, {
            language: chunk.language,
            speaker: currentSpeaker,
          });
          send({
            type: 'audio_chunk',
            audio: result.audio.toString('base64'),
            sampleRate: result.sampleRate,
            generationId,
          });
        } catch (e) {
          send({ type: 'tts_error', message: e.message });
        }
      }
    }

    tts.on('synthesis_complete', function onComplete(event) {
      if (event.generationId === generationId) {
        turnController.onAgentSpeakEnd(generationId);
        turnMetrics.playbackEndTs = monotonicNow();
        const latency = telemetry.recordTurn(turnMetrics);
        send({ type: 'turn_metrics', metrics: latency });
        tts.off('synthesis_complete', onComplete);
      }
    });
  }

  ws.on('message', async (raw) => {
    if (raw.length > MAX_MESSAGE_BYTES) {
      send({ type: 'error', message: 'Message too large' });
      return;
    }

    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      send({ type: 'error', message: 'Invalid JSON' });
      return;
    }

    switch (msg.type) {
      case 'greeting': {
        const greeting = getGreeting();
        messages.push({ role: 'assistant', content: greeting });
        send({ type: 'transcript', role: 'assistant', text: greeting, turnId: 0, generationId: 0 });

        const plan = createSpeakablePlan(greeting, { turnIndex: 0 });
        for (const chunk of plan.chunks) {
          tts.speak(chunk.text, 0);
        }
        break;
      }

      case 'audio': {
        stt.sendAudio(msg.data);
        turnController.onVoiceActivity(true);
        break;
      }

      case 'audio_end': {
        stt.flush();
        turnController.onVoiceActivity(false);
        break;
      }

      case 'vad': {
        turnController.onVoiceActivity(msg.active);
        break;
      }

      case 'text': {
        if (processing) return;
        const text = (msg.text || '').trim();
        if (!text) return;
        send({ type: 'transcript', role: 'user', text, turnId });
        turnController.onFinalTranscript(text);
        break;
      }

      case 'interrupt': {
        generationId++;
        tts.cancelGeneration(generationId - 1);
        turnController.onVoiceActivity(true);
        send({ type: 'interrupted', generationId });
        break;
      }

      case 'set_voice': {
        if (msg.speaker) currentSpeaker = msg.speaker;
        if (msg.language) currentLanguage = msg.language;
        tts.updateVoice({ speaker: currentSpeaker, language: currentLanguage, pace: msg.pace });
        send({ type: 'voice_updated', speaker: currentSpeaker, language: currentLanguage });
        break;
      }

      case 'get_metrics': {
        send({ type: 'session_metrics', metrics: telemetry.exportSanitized() });
        break;
      }

      case 'get_clinic': {
        const { getClinicInfo } = await import('./dental-demo.js');
        send({ type: 'clinic_info', clinic: getClinicInfo() });
        break;
      }

      case 'ping': {
        send({ type: 'pong' });
        break;
      }

      default:
        send({ type: 'error', message: 'Unknown message type: ' + msg.type });
    }
  });

  ws.on('close', () => {
    console.log(`[VOICE-LAB] Session ${sessionId} ended`);
    stt.close();
    tts.close();
    turnController.destroy();
  });

  stt.connect();
  tts.connect();
}
