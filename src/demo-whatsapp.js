import 'dotenv/config';
import { textToSpeech } from './sarvam.js';
import { chat, buildSystemPrompt } from './llm.js';
import { writeFileSync, mkdirSync } from 'fs';

const DEMO_BUSINESS = {
  name: "Dr. Priya's Dental Clinic",
  workingHours: 'Monday to Saturday, 10am-1pm and 5pm-8pm',
  services: 'Consultation ₹500, Cleaning ₹1000, Root Canal ₹3000-5000',
  address: '42 Anna Nagar, Chennai',
  availability: 'Tomorrow: 10am, 11am, 5pm, 6pm available',
};

// Simulate a full call and save each turn as an audio file
async function generateDemo() {
  console.log('\nFonely Demo — Generating audio for WhatsApp\n');
  console.log('============================================\n');

  mkdirSync('demo_audio', { recursive: true });

  const systemPrompt = buildSystemPrompt(DEMO_BUSINESS);
  const messages = [];

  // The conversation turns: [who, text, language]
  const callerTurns = [
    "Oru appointment venum, palla valikkuthu",  // Tamil: I need appointment, tooth pain
    "Tomorrow evening",
    "6pm",
    "Ramesh",
    "Root canal ku evvalavu charge?",           // Tamil: How much for root canal?
    "OK thanks",
  ];

  // Generate greeting first
  const greeting = "Vanakkam! Dr. Priya Dental Clinic. Sollunga, enna help venum?";
  console.log(`  [AI Greeting] "${greeting}"`);
  const greetingAudio = await textToSpeechWav(greeting, 'ta-IN');
  writeFileSync('demo_audio/01_ai_greeting.wav', greetingAudio);
  console.log(`  → Saved: demo_audio/01_ai_greeting.wav\n`);

  let turnNum = 2;
  for (const callerText of callerTurns) {
    // Caller says something
    console.log(`  [Caller] "${callerText}"`);

    // Get AI response
    messages.push({ role: 'user', content: callerText });
    const response = await chat(messages, systemPrompt);
    messages.push({ role: 'assistant', content: response });

    console.log(`  [AI] "${response}"`);

    // Generate audio for AI response
    const audio = await textToSpeechWav(response, 'ta-IN');
    const filename = `demo_audio/${String(turnNum).padStart(2, '0')}_ai_response.wav`;
    writeFileSync(filename, audio);
    console.log(`  → Saved: ${filename}\n`);
    turnNum++;
  }

  console.log('\n============================================');
  console.log('DEMO READY!\n');
  console.log('Files saved in demo_audio/ folder.');
  console.log('\nTo show your mom:');
  console.log('1. Copy demo_audio/*.wav files to your phone');
  console.log('2. Play them in sequence — each one is one AI response');
  console.log('3. Or: combine into one audio file with:');
  console.log('   sox demo_audio/*.wav demo_audio/full_demo.wav');
  console.log('\nAlternatively, send each audio as a WhatsApp voice note.');
}

async function textToSpeechWav(text, language) {
  // Use REST API for demo (returns full audio, easier to save as WAV)
  const res = await fetch('https://api.sarvam.ai/text-to-speech', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'api-subscription-key': process.env.SARVAM_API_KEY,
    },
    body: JSON.stringify({
      text,
      target_language_code: language,
      model: 'bulbul:v3',
      speaker: 'kavitha',  // natural Indian female voice
      speech_sample_rate: '22050',
      output_audio_codec: 'wav',
      pace: 1.0,
    }),
  });

  if (!res.ok) {
    const err = await res.json();
    throw new Error(`TTS error: ${err.error?.message}`);
  }

  const data = await res.json();
  return Buffer.from(data.audios[0], 'base64');
}

generateDemo().catch(console.error);
