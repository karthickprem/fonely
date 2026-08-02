import 'dotenv/config';

const API_KEY = process.env.SARVAM_API_KEY;
console.log('API Key present:', !!API_KEY, 'length:', API_KEY?.length);

// ============================================================
// Step A — REST STT
// ============================================================
async function testRestSTT() {
  console.log('\n=== Step A: REST STT ===');

  // Create a 1-second 16kHz mono PCM silence buffer with a click
  const sampleRate = 16000;
  const duration = 1;
  const samples = sampleRate * duration;
  const pcm = new Int16Array(samples);
  // Add a click at 0.1s to give STT something
  for (let i = 1600; i < 1700; i++) pcm[i] = 10000;

  // Create WAV header
  const wavHeader = Buffer.alloc(44);
  const dataLen = pcm.length * 2;
  wavHeader.write('RIFF', 0);
  wavHeader.writeUInt32LE(36 + dataLen, 4);
  wavHeader.write('WAVE', 8);
  wavHeader.write('fmt ', 12);
  wavHeader.writeUInt32LE(16, 16);
  wavHeader.writeUInt16LE(1, 20);   // PCM
  wavHeader.writeUInt16LE(1, 22);   // mono
  wavHeader.writeUInt32LE(sampleRate, 24);
  wavHeader.writeUInt32LE(sampleRate * 2, 28);
  wavHeader.writeUInt16LE(2, 32);
  wavHeader.writeUInt16LE(16, 34);
  wavHeader.write('data', 36);
  wavHeader.writeUInt32LE(dataLen, 40);

  const wavBuffer = Buffer.concat([wavHeader, Buffer.from(pcm.buffer)]);
  console.log('WAV size:', wavBuffer.length, 'bytes');

  const formData = new FormData();
  formData.append('file', new Blob([wavBuffer], { type: 'audio/wav' }), 'test.wav');
  formData.append('model', 'saaras:v3');
  formData.append('language_code', 'unknown');

  try {
    const res = await fetch('https://api.sarvam.ai/speech-to-text', {
      method: 'POST',
      headers: { 'api-subscription-key': API_KEY },
      body: formData,
    });
    console.log('Status:', res.status);
    const body = await res.text();
    console.log('Response:', body.substring(0, 500));
    return res.ok;
  } catch (e) {
    console.error('Error:', e.message);
    return false;
  }
}

// ============================================================
// Step B — REST TTS
// ============================================================
async function testRestTTS() {
  console.log('\n=== Step B: REST TTS ===');

  try {
    const res = await fetch('https://api.sarvam.ai/text-to-speech', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'api-subscription-key': API_KEY,
      },
      body: JSON.stringify({
        text: 'வணக்கம், Smile Dental Clinic',
        target_language_code: 'ta-IN',
        model: 'bulbul:v3',
        speaker: 'kavitha',
        speech_sample_rate: 8000,
        output_audio_codec: 'linear16',
        pace: 1.0,
      }),
    });
    console.log('Status:', res.status);

    if (res.ok) {
      const data = await res.json();
      const audioB64 = data.audios?.[0];
      if (audioB64) {
        const audioBytes = Buffer.from(audioB64, 'base64');
        console.log('Audio bytes:', audioBytes.length);
        console.log('First 4 bytes (hex):', audioBytes.slice(0, 4).toString('hex'));
        console.log('Format: raw PCM (base64 decoded, linear16)');
        console.log('Sample rate: 8000 (as requested)');

        // Check if it's actually WAV
        const magic = audioBytes.slice(0, 4).toString('ascii');
        console.log('Magic:', JSON.stringify(magic), magic === 'RIFF' ? '→ WAV file' : '→ raw PCM');
      }
      return true;
    } else {
      const body = await res.text();
      console.log('Error:', body.substring(0, 500));
      return false;
    }
  } catch (e) {
    console.error('Error:', e.message);
    return false;
  }
}

// ============================================================
// Step B2 — HTTP Streaming TTS
// ============================================================
async function testStreamTTS() {
  console.log('\n=== Step B2: HTTP Streaming TTS ===');

  try {
    const res = await fetch('https://api.sarvam.ai/text-to-speech/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'api-subscription-key': API_KEY,
      },
      body: JSON.stringify({
        text: 'வணக்கம், Smile Dental Clinic',
        target_language_code: 'ta-IN',
        model: 'bulbul:v3',
        speaker: 'kavitha',
        output_audio_codec: 'linear16',
        pace: 1.0,
      }),
    });
    console.log('Status:', res.status);
    console.log('Content-Type:', res.headers.get('content-type'));

    if (res.ok && res.body) {
      const reader = res.body.getReader();
      let totalBytes = 0;
      let chunkCount = 0;
      let firstChunkBytes = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        chunkCount++;
        totalBytes += value.length;
        if (!firstChunkBytes) {
          firstChunkBytes = Buffer.from(value);
          console.log('First chunk size:', value.length);
          console.log('First 4 bytes (hex):', firstChunkBytes.slice(0, 4).toString('hex'));
          const magic = firstChunkBytes.slice(0, 4).toString('ascii');
          console.log('Magic:', JSON.stringify(magic), magic === 'RIFF' ? '→ WAV' : '→ raw PCM stream');
        }
      }
      console.log('Total chunks:', chunkCount, 'Total bytes:', totalBytes);
      return true;
    } else {
      const body = await res.text();
      console.log('Error:', body.substring(0, 500));
      return false;
    }
  } catch (e) {
    console.error('Error:', e.message);
    return false;
  }
}

// ============================================================
// Step C — WebSocket STT
// ============================================================
async function testWebSocketSTT() {
  console.log('\n=== Step C: WebSocket STT ===');

  const { WebSocket } = await import('ws');

  return new Promise((resolve) => {
    const timeout = setTimeout(() => {
      console.log('Timeout — no response in 10s');
      ws.close();
      resolve(false);
    }, 10000);

    const ws = new WebSocket('wss://api.sarvam.ai/speech-to-text/ws', {
      headers: { 'Api-Subscription-Key': API_KEY },
    });

    ws.on('open', () => {
      console.log('Connected');
      ws.send(JSON.stringify({
        config: {
          model: 'saaras:v3',
          mode: 'transcribe',
          language_code: 'unknown',
          sample_rate: 16000,
          encoding: 'pcm_s16le',
        },
      }));
      console.log('Config sent, sending silence...');

      // Send 2 seconds of silence as PCM
      const silence = Buffer.alloc(16000 * 2 * 2); // 2 sec, 16kHz, 16-bit
      const b64 = silence.toString('base64');
      ws.send(JSON.stringify({
        audio: { data: b64, sample_rate: '16000', encoding: 'pcm_s16le' },
      }));
      console.log('Audio sent, waiting for response...');
    });

    ws.on('message', (raw) => {
      const msg = JSON.parse(raw.toString());
      console.log('Message:', JSON.stringify(msg).substring(0, 300));
    });

    ws.on('close', (code, reason) => {
      clearTimeout(timeout);
      console.log('Closed:', code, reason?.toString());
      resolve(code !== 1000 || false);
    });

    ws.on('error', (err) => {
      clearTimeout(timeout);
      console.error('Error:', err.message);
      resolve(false);
    });
  });
}

// ============================================================
// Run all tests
// ============================================================
async function main() {
  console.log('=== SARVAM API CONNECTIVITY TEST ===');
  console.log('Date:', new Date().toISOString());

  const sttOk = await testRestSTT();
  const ttsOk = await testRestTTS();
  const streamTtsOk = await testStreamTTS();
  const wsSttOk = await testWebSocketSTT();

  console.log('\n=== RESULTS ===');
  console.log('REST STT:', sttOk ? 'WORKS' : 'FAILS');
  console.log('REST TTS:', ttsOk ? 'WORKS' : 'FAILS');
  console.log('Stream TTS:', streamTtsOk ? 'WORKS' : 'FAILS');
  console.log('WebSocket STT:', wsSttOk ? 'WORKS' : 'FAILS');
  console.log('\nDECISION: Use', sttOk ? 'REST STT' : 'NOTHING (STT broken)',
    '+', (streamTtsOk || ttsOk) ? (streamTtsOk ? 'Stream TTS' : 'REST TTS') : 'NOTHING (TTS broken)');
}

main().catch(console.error);
