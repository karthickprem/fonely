import { textToSpeech, textToSpeechStream } from './sarvam.js';
import { chat, buildSystemPrompt } from './llm.js';
import { writeFileSync, unlinkSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

const SARVAM_API_KEY = process.env.SARVAM_API_KEY;

const DEMO_BUSINESS = {
  name: "Dr. Priya's Dental Clinic",
  workingHours: 'Monday to Saturday, 10am-1pm and 5pm-8pm',
  services: 'Consultation ₹500, Cleaning ₹1000, Root Canal ₹3000-5000, Extraction ₹500-1500',
  address: '42 Anna Nagar, Chennai',
  availability: 'Tomorrow: 10am, 11am, 5pm, 6pm available. Day after: 10am, 10:30am, 2pm available.',
};

export function handleBrowserWebSocket(ws) {
  const systemPrompt = buildSystemPrompt(DEMO_BUSINESS);
  const messages = [];
  let isProcessing = false;

  console.log('[BROWSER] New demo session');

  ws.on('message', async (raw) => {
    try {
      const msg = JSON.parse(raw.toString());

      if (msg.type === 'greeting') {
        await sendAIResponse(ws, messages, systemPrompt, null, true);
        return;
      }

      if (msg.type === 'text') {
        if (isProcessing) return;
        isProcessing = true;
        const userText = msg.text.trim();
        if (!userText) { isProcessing = false; return; }
        ws.send(JSON.stringify({ type: 'transcript', role: 'user', text: userText }));
        await sendAIResponse(ws, messages, systemPrompt, userText, false);
        isProcessing = false;
        return;
      }

      if (msg.type === 'audio') {
        if (isProcessing) return;
        isProcessing = true;

        ws.send(JSON.stringify({ type: 'status', text: 'Processing your voice...' }));

        console.log(`[BROWSER] Received audio: ${msg.audio.length} base64 chars, mime: ${msg.mimeType}`);

        const transcript = await transcribeAudio(msg.audio, msg.mimeType || 'audio/webm');

        if (!transcript || transcript.trim().length === 0) {
          console.log('[BROWSER] STT returned empty transcript');
          ws.send(JSON.stringify({ type: 'status', text: 'Could not hear clearly. Try speaking louder or longer.' }));
          isProcessing = false;
          return;
        }

        ws.send(JSON.stringify({ type: 'transcript', role: 'user', text: transcript }));
        await sendAIResponse(ws, messages, systemPrompt, transcript, false);
        isProcessing = false;
      }
    } catch (e) {
      console.error('[BROWSER] Error:', e);
      ws.send(JSON.stringify({ type: 'error', text: e.message }));
      isProcessing = false;
    }
  });

  ws.on('close', () => {
    console.log('[BROWSER] Session ended');
  });
}

async function sendAIResponse(ws, messages, systemPrompt, userText, isGreeting) {
  if (userText) {
    messages.push({ role: 'user', content: userText });
  }

  let aiText;
  if (isGreeting) {
    aiText = "Vanakkam! Dr. Priya Dental Clinic. Sollunga, enna help venum?";
  } else {
    ws.send(JSON.stringify({ type: 'status', text: 'Thinking...' }));
    const start = Date.now();
    aiText = await chat(messages, systemPrompt);
    console.log(`[BROWSER] LLM (${Date.now() - start}ms): "${aiText}"`);
  }

  messages.push({ role: 'assistant', content: aiText });
  ws.send(JSON.stringify({ type: 'transcript', role: 'assistant', text: aiText }));
  ws.send(JSON.stringify({ type: 'status', text: 'Speaking...' }));

  const start = Date.now();

  // Try streaming TTS first (low latency), fall back to REST
  try {
    const chunks = [];
    const stats = await textToSpeechStream(aiText, 'ta-IN', 'kavitha', (chunk) => {
      // Send each audio chunk immediately as it arrives
      const b64 = chunk.toString('base64');
      ws.send(JSON.stringify({ type: 'audio_chunk', audio: b64, format: 'linear16', sampleRate: 24000 }));
      chunks.push(chunk);
    });
    console.log(`[BROWSER] TTS Streaming — first chunk: ${stats.firstChunkMs}ms, total: ${stats.totalMs}ms`);
    ws.send(JSON.stringify({ type: 'audio_done' }));
  } catch (e) {
    console.log(`[BROWSER] Streaming TTS failed (${e.message}), falling back to REST`);
    const audioBase64 = await textToSpeech(aiText, 'ta-IN', 'kavitha');
    console.log(`[BROWSER] TTS REST (${Date.now() - start}ms)`);
    ws.send(JSON.stringify({ type: 'audio', audio: audioBase64, format: 'linear16', sampleRate: 8000 }));
  }
}

async function transcribeAudio(base64Audio, mimeType) {
  const audioBuffer = Buffer.from(base64Audio, 'base64');
  console.log(`[STT] Audio buffer size: ${audioBuffer.length} bytes`);

  // Save to temp file — more reliable than FormData Blob
  const tmpPath = join('/scratch/karthick/fonely', `.tmp_audio_${Date.now()}.webm`);
  writeFileSync(tmpPath, audioBuffer);

  try {
    // Use curl for reliable multipart upload
    const { execSync } = await import('child_process');
    const result = execSync(`curl -s -X POST "https://api.sarvam.ai/speech-to-text" \
      -H "api-subscription-key: ${SARVAM_API_KEY}" \
      -F "file=@${tmpPath};type=${mimeType}" \
      -F "model=saaras:v3" \
      -F "language_code=unknown"`, {
      timeout: 30000,
      encoding: 'utf-8',
    });

    console.log(`[STT] Raw response: ${result.substring(0, 200)}`);

    const data = JSON.parse(result);
    if (data.error) {
      console.error(`[STT] API error: ${data.error.message}`);
      return null;
    }

    console.log(`[STT] Transcript: "${data.transcript}" (lang: ${data.language_code})`);
    return data.transcript;
  } catch (e) {
    console.error(`[STT] Error: ${e.message}`);
    return null;
  } finally {
    try { unlinkSync(tmpPath); } catch {}
  }
}
