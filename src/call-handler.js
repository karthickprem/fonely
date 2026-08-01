import { createSTTStream, textToSpeechStream } from './sarvam.js';
import { chat, buildSystemPrompt } from './llm.js';

const activeCalls = new Map();

const DEMO_BUSINESS = {
  name: "Dr. Priya's Dental Clinic",
  workingHours: 'Monday to Saturday, 10am-1pm and 5pm-8pm',
  services: 'Consultation ₹500, Cleaning ₹1000, Root Canal ₹3000-5000, Extraction ₹500-1500',
  address: '42 Anna Nagar, Chennai',
  availability: 'Tomorrow: 10am, 11am, 5pm, 6pm available. Day after: 10am, 10:30am, 2pm available.',
};

export function handleExotelWebSocket(ws) {
  let callState = null;

  ws.on('message', async (raw) => {
    try {
      const msg = JSON.parse(raw.toString());

      switch (msg.event) {
        case 'start':
          callState = initCall(msg, ws);
          console.log(`[CALL] Started: ${callState.callSid} from ${callState.callerPhone}`);
          await sendGreeting(callState);
          break;

        case 'media':
          if (callState) {
            callState.sttStream.sendAudio(msg.media.payload);
          }
          break;

        case 'stop':
          console.log(`[CALL] Ended: ${callState?.callSid} reason=${msg.stop?.reason}`);
          endCall(callState);
          break;

        default:
          break;
      }
    } catch (e) {
      console.error('[CALL] Error processing message:', e.message);
    }
  });

  ws.on('close', () => {
    if (callState) endCall(callState);
  });

  ws.on('error', (err) => {
    console.error('[CALL] WebSocket error:', err.message);
  });
}

function initCall(startMsg, exotelWs) {
  const { call_sid, from, to, stream_sid } = startMsg.start;
  const systemPrompt = buildSystemPrompt(DEMO_BUSINESS);

  const state = {
    callSid: call_sid,
    streamSid: stream_sid,
    callerPhone: from,
    businessPhone: to,
    exotelWs,
    messages: [],
    systemPrompt,
    isResponding: false,
    pendingTranscripts: [],
    sttStream: null,
  };

  state.sttStream = createSTTStream('unknown', (transcript) => {
    onCallerSpeech(state, transcript);
  });

  activeCalls.set(call_sid, state);
  return state;
}

async function sendGreeting(state) {
  const greeting = "Vanakkam! Dr. Priya Dental Clinic. Enna help venum?";
  console.log(`[AI] Greeting: "${greeting}"`);
  await streamAudioToExotel(state, greeting, 'ta-IN');
}

async function onCallerSpeech(state, transcript) {
  if (state.isResponding) {
    state.pendingTranscripts.push(transcript);
    return;
  }

  console.log(`[CALLER] "${transcript}"`);
  state.messages.push({ role: 'user', content: transcript });

  state.isResponding = true;
  try {
    const startTime = Date.now();
    const response = await chat(state.messages, state.systemPrompt);
    const llmMs = Date.now() - startTime;
    console.log(`[AI] (${llmMs}ms) "${response}"`);

    state.messages.push({ role: 'assistant', content: response });

    const lang = detectLanguageHint(transcript);
    await streamAudioToExotel(state, response, lang);
  } catch (e) {
    console.error('[CALL] Response error:', e.message);
    await streamAudioToExotel(state, "Sorry, oru nimisham. Please try again.", 'ta-IN');
  }
  state.isResponding = false;

  if (state.pendingTranscripts.length > 0) {
    const next = state.pendingTranscripts.shift();
    onCallerSpeech(state, next);
  }
}

async function streamAudioToExotel(state, text, language) {
  const CHUNK_SIZE = 3200; // 200ms at 8kHz 16-bit mono

  try {
    const stats = await textToSpeechStream(text, language, 'priya', (audioChunk) => {
      // Stream each TTS chunk directly to Exotel as it arrives
      for (let i = 0; i < audioChunk.length; i += CHUNK_SIZE) {
        const chunk = audioChunk.subarray(i, Math.min(i + CHUNK_SIZE, audioChunk.length));
        // Pad to CHUNK_SIZE if needed (Exotel requires multiples of 320)
        let sendChunk = chunk;
        if (chunk.length % 320 !== 0) {
          const padded = Buffer.alloc(Math.ceil(chunk.length / 320) * 320);
          chunk.copy(padded);
          sendChunk = padded;
        }

        const msg = {
          event: 'media',
          stream_sid: state.streamSid,
          media: { payload: sendChunk.toString('base64') },
        };

        if (state.exotelWs.readyState === 1) {
          state.exotelWs.send(JSON.stringify(msg));
        }
      }
    });

    if (stats) {
      console.log(`[TTS] First chunk: ${stats.firstChunkMs}ms, Total: ${stats.totalMs}ms`);
    }
  } catch (e) {
    console.error('[TTS] Stream error:', e.message);
  }
}

function detectLanguageHint(text) {
  if (/[஀-௿]/.test(text)) return 'ta-IN';
  if (/[ऀ-ॿ]/.test(text)) return 'hi-IN';
  if (/[ఀ-౿]/.test(text)) return 'te-IN';
  if (/[ಀ-೿]/.test(text)) return 'kn-IN';
  if (/[ം-ൿ]/.test(text)) return 'ml-IN';
  if (/[଀-୿]/.test(text)) return 'od-IN';
  if (/[ਁ-੿]/.test(text)) return 'pa-IN';
  if (/[ઁ-૿]/.test(text)) return 'gu-IN';
  if (/[ং-৿]/.test(text)) return 'bn-IN';
  return 'en-IN';
}

function endCall(state) {
  if (!state) return;
  state.sttStream?.close();
  activeCalls.delete(state.callSid);

  console.log(`[CALL] === Call Summary: ${state.callSid} ===`);
  console.log(`  Caller: ${state.callerPhone}`);
  console.log(`  Turns: ${state.messages.length}`);
  state.messages.forEach((m) => {
    console.log(`  [${m.role}] ${m.content}`);
  });
  console.log(`[CALL] === End ===`);
}

export function getActiveCalls() {
  return activeCalls.size;
}
