import { applyLexicon } from './pronunciation.js';

const TAMIL_DIGITS = ['பூஜ்ஜியம்', 'ஒன்று', 'இரண்டு', 'மூன்று', 'நான்கு', 'ஐந்து', 'ஆறு', 'ஏழு', 'எட்டு', 'ஒன்பது'];
const TAMIL_TENS = ['', 'பத்து', 'இருபது', 'முப்பது', 'நாற்பது', 'ஐம்பது', 'அறுபது', 'எழுபது', 'எண்பது', 'தொண்ணூறு'];

function tamilNumber(n) {
  if (n < 0) return 'மைனஸ் ' + tamilNumber(-n);
  if (n < 10) return TAMIL_DIGITS[n];
  if (n === 100) return 'நூறு';
  if (n === 1000) return 'ஆயிரம்';
  if (n < 100) {
    const tens = Math.floor(n / 10);
    const ones = n % 10;
    if (ones === 0) return TAMIL_TENS[tens];
    return TAMIL_TENS[tens] + ' ' + TAMIL_DIGITS[ones];
  }
  if (n < 1000) {
    const hundreds = Math.floor(n / 100);
    const remainder = n % 100;
    const prefix = hundreds === 1 ? 'நூற்று' :
      hundreds === 2 ? 'இருநூற்று' :
      hundreds === 3 ? 'முன்னூற்று' :
      hundreds === 4 ? 'நானூற்று' :
      hundreds === 5 ? 'ஐநூற்று' :
      hundreds === 6 ? 'அறுநூற்று' :
      hundreds === 7 ? 'எழுநூற்று' :
      hundreds === 8 ? 'எண்ணூற்று' :
      'தொள்ளாயிரத்து';
    if (remainder === 0) return prefix.replace(/த்து$/, 'று');
    return prefix + ' ' + tamilNumber(remainder);
  }
  if (n < 100000) {
    const thousands = Math.floor(n / 1000);
    const remainder = n % 1000;
    const prefix = thousands === 1 ? 'ஆயிரத்து' :
      tamilNumber(thousands) + ' ஆயிரத்து';
    if (remainder === 0) return prefix.replace(/த்து$/, 'ம்');
    return prefix + ' ' + tamilNumber(remainder);
  }
  return String(n);
}

export function normalizeCurrency(text, lang = 'ta') {
  return text.replace(/₹\s?([\d,]+(?:\.\d{2})?)/g, (_, amount) => {
    const num = parseInt(amount.replace(/,/g, ''), 10);
    if (lang === 'ta' || lang === 'ta-IN') {
      return tamilNumber(num) + ' ரூபாய்';
    }
    return num.toLocaleString('en-IN') + ' rupees';
  });
}

export function normalizeTime(text, lang = 'ta') {
  return text.replace(/(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?/g, (_, h, m, period) => {
    const hour = parseInt(h, 10);
    const minute = parseInt(m, 10);
    if (lang === 'ta' || lang === 'ta-IN') {
      if (minute === 0) return tamilNumber(hour) + ' மணி';
      if (minute === 30) return tamilNumber(hour) + ' மணி முப்பது';
      return tamilNumber(hour) + ' மணி ' + tamilNumber(minute);
    }
    const suffix = period ? ' ' + period.toUpperCase() : '';
    if (minute === 0) return hour + suffix;
    if (minute === 30) return hour + ' thirty' + suffix;
    return `${hour}:${m}${suffix}`;
  });
}

export function normalizeDate(text, lang = 'ta') {
  const tamilDays = {
    monday: 'திங்கள்', tuesday: 'செவ்வாய்', wednesday: 'புதன்',
    thursday: 'வியாழன்', friday: 'வெள்ளி', saturday: 'சனி', sunday: 'ஞாயிறு',
    mon: 'திங்கள்', tue: 'செவ்வாய்', wed: 'புதன்', thu: 'வியாழன்',
    fri: 'வெள்ளி', sat: 'சனி', sun: 'ஞாயிறு',
  };
  if (lang === 'ta' || lang === 'ta-IN') {
    return text.replace(/\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b/gi,
      (match) => tamilDays[match.toLowerCase()] || match);
  }
  return text;
}

export function normalizePhoneDigits(text, lang = 'ta') {
  return text.replace(/(\d{10})/g, (match) => {
    if (lang === 'ta' || lang === 'ta-IN') {
      return match.split('').map((d) => TAMIL_DIGITS[parseInt(d, 10)]).join(' ');
    }
    return match.split('').join(' ');
  });
}

export function detectLanguage(text) {
  const tamilPattern = /[஀-௿]/;
  const englishPattern = /[a-zA-Z]/;
  const hasTamil = tamilPattern.test(text);
  const hasEnglish = englishPattern.test(text);
  if (hasTamil && hasEnglish) return 'tanglish';
  if (hasTamil) return 'ta';
  return 'en';
}

export function chunkAtSentenceBoundaries(text) {
  const chunks = [];
  const sentences = text.split(/(?<=[.!?।])\s+|(?<=\n)\s*/);
  let current = '';

  for (const sentence of sentences) {
    const trimmed = sentence.trim();
    if (!trimmed) continue;

    if (current && (current + ' ' + trimmed).length > 120) {
      chunks.push(current.trim());
      current = trimmed;
    } else {
      current = current ? current + ' ' + trimmed : trimmed;
    }
  }
  if (current.trim()) chunks.push(current.trim());
  return chunks.length > 0 ? chunks : [text];
}

export function inferEmotion(text, turnIndex) {
  if (turnIndex === 0) return 'warm_greeting';
  const lower = text.toLowerCase();
  if (lower.includes('sorry') || lower.includes('மன்னிக்கவும்')) return 'empathetic_recovery';
  if (lower.includes('confirm') || lower.includes('booked') || lower.includes('பதிவு')) return 'clear_confirmation';
  if (lower.includes('done') || lower.includes('great') || lower.includes('சரி')) return 'positive_success';
  if (lower.includes('?') || lower.includes('எப்போது') || lower.includes('என்ன')) return 'helpful_question';
  return 'neutral_information';
}

export function createSpeakablePlan(text, options = {}) {
  const lang = options.lang || detectLanguage(text);
  const turnIndex = options.turnIndex ?? 0;
  const ttsLang = lang === 'en' ? 'en' : 'ta';

  let processed = text;
  processed = normalizeCurrency(processed, ttsLang);
  processed = normalizeTime(processed, ttsLang);
  processed = normalizeDate(processed, ttsLang);
  processed = normalizePhoneDigits(processed, ttsLang);

  if (ttsLang === 'ta') {
    processed = applyLexicon(processed, 'ta');
  }

  const chunks = chunkAtSentenceBoundaries(processed);
  const emotion = inferEmotion(text, turnIndex);

  return {
    originalText: text,
    detectedLanguage: lang,
    ttsLanguage: lang === 'en' ? 'en-IN' : 'ta-IN',
    emotion,
    chunks: chunks.map((chunk, i) => ({
      text: chunk,
      language: lang === 'en' ? 'en-IN' : 'ta-IN',
      emotion: i === 0 ? emotion : 'neutral_information',
      interruptible: true,
    })),
  };
}
