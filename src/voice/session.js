/*
  Sarvam API connectivity results (2026-08-02):
    REST STT:       WORKS (POST multipart WAV → JSON {transcript, language_code})
    REST TTS:       WORKS (POST JSON → JSON {audios[0]: base64 PCM})
    Stream TTS:     WORKS (POST JSON → raw PCM stream, content-type audio/pcm)
    WebSocket STT:  FAILS (error: "audio must not be None" — wrong frame format)
    WebSocket TTS:  FAILS (closes immediately after config, code 1000)

  Decision: REST STT + REST TTS. Audio format is raw PCM signed 16-bit LE.
*/

import { TurnController } from './turn-controller.js';
import { createSpeakablePlan } from './speakable-plan.js';
import { chatWithLLM, checkSafetyRules, getGreeting } from './dental-demo.js';
import { createTurnMetrics, monotonicNow, SessionTelemetry } from './telemetry.js';

const SARVAM_API_KEY = process.env.SARVAM_API_KEY;
const TTS_SAMPLE_RATE = 22050;

// --- REST STT ---
async function transcribe(pcmBuffer) {
  const wavHeader = Buffer.alloc(44);
  const dataLen = pcmBuffer.length;
  wavHeader.write('RIFF', 0);
  wavHeader.writeUInt32LE(36 + dataLen, 4);
  wavHeader.write('WAVE', 8);
  wavHeader.write('fmt ', 12);
  wavHeader.writeUInt32LE(16, 16);
  wavHeader.writeUInt16LE(1, 20);
  wavHeader.writeUInt16LE(1, 22);
  wavHeader.writeUInt32LE(16000, 24);
  wavHeader.writeUInt32LE(32000, 28);
  wavHeader.writeUInt16LE(2, 32);
  wavHeader.writeUInt16LE(16, 34);
  wavHeader.write('data', 36);
  wavHeader.writeUInt32LE(dataLen, 40);

  const wav = Buffer.concat([wavHeader, pcmBuffer]);
  const form = new FormData();
  form.append('file', new Blob([wav], { type: 'audio/wav' }), 'audio.wav');
  form.append('model', 'saaras:v3');
  form.append('language_code', 'unknown');

  const res = await fetch('https://api.sarvam.ai/speech-to-text', {
    method: 'POST',
    headers: { 'api-subscription-key': SARVAM_API_KEY },
    body: form,
  });

  if (!res.ok) throw new Error('STT HTTP ' + res.status);
  const data = await res.json();
  return { transcript: (data.transcript || '').trim(), language: data.language_code };
}

// --- REST TTS ---
async function synthesize(text, language, speaker) {
  const res = await fetch('https://api.sarvam.ai/text-to-speech', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'api-subscription-key': SARVAM_API_KEY,
    },
    body: JSON.stringify({
      text,
      target_language_code: language,
      model: 'bulbul:v3',
      speaker,
      speech_sample_rate: TTS_SAMPLE_RATE,
      output_audio_codec: 'linear16',
      pace: 1.0,
    }),
  });

  if (!res.ok) throw new Error('TTS HTTP ' + res.status + ': ' + (await res.text()).substring(0, 200));
  const data = await res.json();
  if (!data.audios?.[0]) throw new Error('TTS returned no audio');
  return data.audios[0]; // base64 PCM
}

// --- Session handler ---
export function handleVoiceLabSession(ws) {
  const sessionId = crypto.randomUUID();
  const telemetry = new SessionTelemetry(sessionId);
  const messages = [];
  let turnId = 0;
  let generationId = 0;
  let currentSpeaker = 'kavitha';
  let currentLanguage = 'ta-IN';
  let processing = false;
  let agentSpeaking = false;

  const send = (msg) => {
    if (ws.readyState === 1) ws.send(JSON.stringify(msg));
  };

  console.log(`[SESSION] ${sessionId.substring(0, 8)} started`);
  send({ type: 'session_start', sessionId });

  // --- Speak a text and send audio to browser ---
  async function speak(text, genId) {
    agentSpeaking = true;
    send({ type: 'agent_speaking', speaking: true });
    try {
      const plan = createSpeakablePlan(text, {
        lang: currentLanguage.startsWith('ta') ? 'ta' : 'en',
        turnIndex: turnId,
      });

      for (const chunk of plan.chunks) {
        if (generationId > genId) break;
        const t0 = monotonicNow();
        const audioB64 = await synthesize(chunk.text, chunk.language, currentSpeaker);
        const ms = (monotonicNow() - t0).toFixed(0);
        const bytes = Math.round(audioB64.length * 0.75);
        console.log(`[TTS] ${ms}ms, ${bytes}b: "${chunk.text.substring(0, 50)}"`);
        send({
          type: 'audio',
          audio: audioB64,
          sampleRate: TTS_SAMPLE_RATE,
          generationId: genId,
        });
      }
      send({ type: 'audio_done', generationId: genId });
    } catch (e) {
      console.error('[TTS] Error:', e.message);
      send({ type: 'error', message: 'TTS failed: ' + e.message });
    } finally {
      agentSpeaking = false;
      send({ type: 'agent_speaking', speaking: false });
    }
  }

  // --- Process a user turn ---
  async function processUserTurn(transcript, sttLanguage) {
    if (processing) return;
    processing = true;
    turnId++;
    generationId++;
    const myGen = generationId;
    const turnMetrics = createTurnMetrics(turnId, myGen);
    turnMetrics.micEndTs = monotonicNow();

    try {
      send({ type: 'transcript', role: 'user', text: transcript, language: sttLanguage });

      // Safety check (deterministic)
      const safety = checkSafetyRules(transcript);
      let responseText;

      if (!safety.safe) {
        responseText = currentLanguage.startsWith('ta') ? safety.responseTa : safety.response;
        send({ type: 'safety_triggered', safetyType: safety.type });
      } else {
        messages.push({ role: 'user', content: transcript });
        turnMetrics.llmStartTs = monotonicNow();
        send({ type: 'status', text: 'Thinking...' });
        responseText = await chatWithLLM(messages);
        turnMetrics.llmEndTs = monotonicNow();
        messages.push({ role: 'assistant', content: responseText });
      }

      console.log(`[LLM] "${responseText.substring(0, 80)}"`);
      send({ type: 'transcript', role: 'assistant', text: responseText });

      turnMetrics.ttsFirstAudioTs = monotonicNow();
      await speak(responseText, myGen);
      turnMetrics.playbackEndTs = monotonicNow();

      const latency = telemetry.recordTurn(turnMetrics);
      send({ type: 'turn_metrics', metrics: latency });
    } catch (e) {
      console.error('[TURN] Error:', e.message);
      send({ type: 'error', message: e.message });
    } finally {
      processing = false;
      send({ type: 'status', text: 'Ready — click mic or type' });
    }
  }

  // --- WebSocket message handler ---
  ws.on('message', async (raw) => {
    let msg;
    try { msg = JSON.parse(raw.toString()); } catch { return; }

    switch (msg.type) {
      case 'greeting': {
        const greeting = getGreeting();
        messages.push({ role: 'assistant', content: greeting });
        send({ type: 'transcript', role: 'assistant', text: greeting });
        speak(greeting, 0);
        break;
      }

      case 'audio_complete': {
        if (processing || agentSpeaking) {
          send({ type: 'status', text: 'Please wait...' });
          break;
        }
        const pcm = Buffer.from(msg.audio, 'base64');
        console.log(`[STT] Received ${pcm.length} bytes`);

        if (pcm.length < 6400) { // < 0.2s at 16kHz
          send({ type: 'status', text: 'Too short — speak longer' });
          break;
        }

        send({ type: 'status', text: 'Transcribing...' });
        try {
          const t0 = monotonicNow();
          const result = await transcribe(pcm);
          const ms = (monotonicNow() - t0).toFixed(0);
          console.log(`[STT] ${ms}ms: "${result.transcript}" [${result.language}]`);

          if (!result.transcript) {
            send({ type: 'status', text: 'Could not hear clearly — try again' });
            break;
          }

          send({ type: 'stt_result', transcript: result.transcript, language: result.language });
          await processUserTurn(result.transcript, result.language);
        } catch (e) {
          console.error('[STT] Error:', e.message);
          send({ type: 'error', message: 'STT failed: ' + e.message });
          send({ type: 'status', text: 'STT error — try again' });
        }
        break;
      }

      case 'text': {
        const text = (msg.text || '').trim();
        if (!text) break;
        await processUserTurn(text, null);
        break;
      }

      case 'interrupt': {
        generationId++;
        agentSpeaking = false;
        send({ type: 'interrupted', generationId });
        break;
      }

      case 'set_voice': {
        if (msg.speaker) currentSpeaker = msg.speaker;
        if (msg.language) currentLanguage = msg.language;
        send({ type: 'voice_updated', speaker: currentSpeaker, language: currentLanguage });
        break;
      }

      case 'get_metrics': {
        send({ type: 'session_metrics', metrics: telemetry.exportSanitized() });
        break;
      }
    }
  });

  ws.on('close', () => {
    console.log(`[SESSION] ${sessionId.substring(0, 8)} ended`);
  });
}
