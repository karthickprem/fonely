const SARVAM_LLM_URL = 'https://api.sarvam.ai/v1/chat/completions';

export const DEMO_CLINIC = {
  name: 'Smile Dental Clinic',
  location: 'Aminjikarai, Chennai',
  doctors: [
    {
      name: 'Dr. Priya',
      specializations: ['general', 'root canal', 'scaling', 'extraction'],
      schedule: 'Mon-Sat',
    },
    {
      name: 'Dr. Arjun',
      specializations: ['orthodontics', 'general'],
      schedule: 'Mon, Wed, Fri',
    },
  ],
  hours: {
    morning: { start: '10:00', end: '13:00' },
    evening: { start: '17:00', end: '20:30' },
  },
  days: 'Monday to Saturday',
  services: [
    { name: 'General Consultation', price: 300, duration: 20 },
    { name: 'Root Canal', priceMin: 3500, priceMax: 5500, duration: 60 },
    { name: 'Scaling', price: 800, duration: 30 },
    { name: 'Extraction', priceMin: 500, priceMax: 1500, duration: 30 },
    { name: 'Orthodontics Consultation', price: 500, duration: 30 },
  ],
  syntheticSlots: {
    tomorrow: ['10:00', '11:00', '14:00', '17:00', '18:30', '19:30'],
    dayAfter: ['10:00', '10:30', '17:00', '18:00', '19:00', '20:00'],
  },
};

const SYSTEM_PROMPT = `You are the virtual receptionist for Smile Dental Clinic, Aminjikarai, Chennai. Your name is Fonely.

PERSONALITY AND STYLE:
- Speak warmly and naturally, like a friendly receptionist at a local Chennai dental clinic
- Match the caller's language: Tamil, Tanglish (Tamil+English mix), or Indian English
- Keep each response to 1-2 short spoken sentences maximum
- Ask only one question at a time
- Use natural acknowledgements like "சரி", "okay", "hmm"
- Sound caring when someone mentions pain or worry

LANGUAGE RULES:
- If the caller speaks in Tamil or Tanglish, respond in natural Tanglish (the way Chennai people actually speak)
- If the caller speaks in English, respond in natural Indian English
- Code-switching is natural and expected — don't force pure Tamil or pure English
- Use Tamil script for Tamil words in your response

CLINIC INFORMATION:
- Smile Dental Clinic, Aminjikarai, Chennai
- Doctors: Dr. Priya (general, root canal, scaling, extraction) — Mon to Sat
           Dr. Arjun (orthodontics, general) — Mon, Wed, Fri
- Hours: 10:00 AM to 1:00 PM, 5:00 PM to 8:30 PM, Monday to Saturday
- Services and fees:
  General Consultation: ₹300 (20 min)
  Root Canal: ₹3,500 to ₹5,500 (60 min)
  Scaling/Cleaning: ₹800 (30 min)
  Extraction: ₹500 to ₹1,500 (30 min)
  Orthodontics Consultation: ₹500 (30 min)

AVAILABLE SLOTS:
Tomorrow: 10:00 AM, 11:00 AM, 2:00 PM, 5:00 PM, 6:30 PM, 7:30 PM
Day after: 10:00 AM, 10:30 AM, 5:00 PM, 6:00 PM, 7:00 PM, 8:00 PM

CONVERSATION FLOW:
1. Greet warmly and ask how you can help
2. Understand what they need (appointment, question about services, fees)
3. Offer 2-3 suitable time slots
4. Confirm: name, date, time, and service
5. Give a positive closing

SAFETY RULES (NEVER BREAK THESE):
- NEVER give medical advice, diagnosis, or treatment recommendations
- NEVER suggest medicines or dosages
- NEVER interpret symptoms or X-rays
- If asked about symptoms/medical issues, say: "I cannot give medical advice. Let me connect you with the clinic staff."
- If emergency keywords (severe pain, bleeding, swelling, accident): "Please visit the clinic immediately or call emergency services. I'll alert the doctor."
- NEVER say "I am an AI" or "As an AI assistant"
- NEVER list more than 3 options at once
- NEVER make up information not provided above`;

const EMERGENCY_KEYWORDS = [
  'bleeding', 'blood', 'accident', 'severe pain', 'swelling',
  'ரத்தம்', 'விபத்து', 'கடுமையான வலி', 'வீக்கம்',
];

const MEDICAL_ADVICE_PATTERNS = [
  /what (medicine|tablet|pill|drug)/i,
  /should i take/i,
  /is it (cancer|serious|dangerous)/i,
  /what is wrong with/i,
  /diagnos/i,
  /x-?ray/i,
  /மருந்து என்ன/,
  /என்ன நோய்/,
];

export function checkSafetyRules(text) {
  const lower = text.toLowerCase();

  for (const keyword of EMERGENCY_KEYWORDS) {
    if (lower.includes(keyword.toLowerCase())) {
      return {
        safe: false,
        type: 'emergency',
        response: 'Please visit the clinic immediately or call emergency services. I will alert the doctor right away.',
        responseTa: 'தயவுசெய்து உடனடியாக கிளினிக்கிற்கு வாருங்கள் அல்லது அவசர சேவைகளை அழையுங்கள். நான் டாக்டருக்கு உடனே தகவல் சொல்கிறேன்.',
      };
    }
  }

  for (const pattern of MEDICAL_ADVICE_PATTERNS) {
    if (pattern.test(text)) {
      return {
        safe: false,
        type: 'medical_advice',
        response: 'I cannot give medical advice. Let me connect you with the clinic staff who can help you properly.',
        responseTa: 'நான் medical advice தர முடியாது. Clinic staff-கிட்ட connect பண்றேன், அவங்க properly help பண்ணுவாங்க.',
      };
    }
  }

  return { safe: true };
}

export async function chatWithLLM(messages, options = {}) {
  const apiKey = options.apiKey || process.env.SARVAM_API_KEY;

  const fullMessages = [
    { role: 'system', content: SYSTEM_PROMPT },
    ...messages,
  ];

  const res = await fetch(SARVAM_LLM_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'api-subscription-key': apiKey,
    },
    body: JSON.stringify({
      model: 'sarvam-105b',
      messages: fullMessages,
      max_tokens: 150,
      temperature: 0.7,
    }),
  });

  if (!res.ok) {
    const err = await res.text();
    console.error('[DENTAL-DEMO] LLM error:', err.substring(0, 200));
    return 'Sorry, one moment please. Can you say that again?';
  }

  const data = await res.json();
  return (data.choices[0]?.message?.content || '').trim().replace(/^\n+/, '');
}

export function getGreeting() {
  return 'வணக்கம்! Smile Dental Clinic. நான் Fonely, virtual receptionist. எப்படி help பண்ணலாம்?';
}

export function getClinicInfo() {
  return DEMO_CLINIC;
}
