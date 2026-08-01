import 'dotenv/config';
import { textToSpeech } from './sarvam.js';
import { chat, buildSystemPrompt } from './llm.js';
import { writeFileSync } from 'fs';

const DEMO_BUSINESS = {
  name: "Dr. Priya's Dental Clinic",
  workingHours: 'Monday to Saturday, 10am-1pm and 5pm-8pm',
  services: 'Consultation ₹500, Cleaning ₹1000, Root Canal ₹3000-5000',
  address: '42 Anna Nagar, Chennai',
  availability: 'Tomorrow: 10am, 11am, 5pm, 6pm available',
};

async function testTTS() {
  console.log('\n=== Test 1: Text-to-Speech ===\n');

  const tests = [
    { text: "Vanakkam, Dr. Priya's Dental Clinic. How can I help you?", lang: 'ta-IN', speaker: 'priya', file: 'test_greeting_tamil.wav' },
    { text: "Namaste, Dr. Priya's Dental Clinic. Main aapki kya madad kar sakta hoon?", lang: 'hi-IN', speaker: 'priya', file: 'test_greeting_hindi.wav' },
    { text: "Hello, Dr. Priya's Dental Clinic. How can I help you?", lang: 'en-IN', speaker: 'priya', file: 'test_greeting_english.wav' },
  ];

  for (const t of tests) {
    try {
      console.log(`  Testing ${t.lang}: "${t.text.substring(0, 50)}..."`);
      const start = Date.now();
      const audioB64 = await textToSpeech(t.text, t.lang, t.speaker);
      const elapsed = Date.now() - start;
      const audioBytes = Buffer.from(audioB64, 'base64');
      writeFileSync(`test_output/${t.file}`, audioBytes);
      console.log(`  ✓ ${t.lang}: ${audioBytes.length} bytes, ${elapsed}ms`);
    } catch (e) {
      console.log(`  ✗ ${t.lang}: ${e.message}`);
    }
  }
}

async function testLLM() {
  console.log('\n=== Test 2: LLM Conversation ===\n');

  const systemPrompt = buildSystemPrompt(DEMO_BUSINESS);
  const conversation = [
    "I have a toothache and need an appointment",
    "Tomorrow evening would be good",
    "6pm please",
    "My name is Ramesh",
    "What are your fees for root canal?",
    "Thank you, that's all",
  ];

  const messages = [];

  for (const userMsg of conversation) {
    console.log(`  Caller: "${userMsg}"`);
    messages.push({ role: 'user', content: userMsg });

    const start = Date.now();
    const response = await chat(messages, systemPrompt);
    const elapsed = Date.now() - start;

    console.log(`  AI (${elapsed}ms): "${response}"\n`);
    messages.push({ role: 'assistant', content: response });
  }
}

async function testEndToEnd() {
  console.log('\n=== Test 3: End-to-End (LLM → TTS) ===\n');

  const systemPrompt = buildSystemPrompt(DEMO_BUSINESS);
  const messages = [
    { role: 'user', content: 'I need an appointment tomorrow' },
  ];

  console.log('  Step 1: Getting LLM response...');
  const start1 = Date.now();
  const response = await chat(messages, systemPrompt);
  const llmTime = Date.now() - start1;
  console.log(`  LLM (${llmTime}ms): "${response}"`);

  console.log('  Step 2: Converting to speech...');
  const start2 = Date.now();
  const audioB64 = await textToSpeech(response, 'ta-IN', 'priya');
  const ttsTime = Date.now() - start2;
  const audioBytes = Buffer.from(audioB64, 'base64');
  writeFileSync('test_output/test_e2e_response.wav', audioBytes);
  console.log(`  TTS (${ttsTime}ms): ${audioBytes.length} bytes`);

  console.log(`\n  Total response time: ${llmTime + ttsTime}ms`);
  console.log(`  Target: < 2000ms`);
  console.log(`  Result: ${llmTime + ttsTime < 2000 ? '✓ PASS' : '✗ NEEDS OPTIMIZATION'}`);
}

async function main() {
  console.log('Fonely — Sarvam API Test Suite');
  console.log('==============================');

  const { mkdirSync } = await import('fs');
  try { mkdirSync('test_output', { recursive: true }); } catch {}

  await testTTS();
  await testLLM();
  await testEndToEnd();

  console.log('\n✓ All tests complete. Check test_output/ for audio files.');
}

main().catch(console.error);
