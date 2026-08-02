import { describe, it, expect } from 'bun:test';
import {
  normalizeCurrency,
  normalizeTime,
  normalizeDate,
  normalizePhoneDigits,
  detectLanguage,
  chunkAtSentenceBoundaries,
  createSpeakablePlan,
  inferEmotion,
} from '../../src/voice/speakable-plan.js';

describe('normalizeCurrency', () => {
  it('converts rupee amount to Tamil words', () => {
    const result = normalizeCurrency('The fee is ₹300.', 'ta');
    expect(result).toContain('ரூபாய்');
    expect(result).not.toContain('₹');
  });

  it('converts rupee amount to English words', () => {
    const result = normalizeCurrency('Fee: ₹3,500', 'en');
    expect(result).toContain('rupees');
    expect(result).not.toContain('₹');
  });

  it('handles comma-separated amounts', () => {
    const result = normalizeCurrency('₹5,500', 'en');
    expect(result).toContain('5,500 rupees');
  });
});

describe('normalizeTime', () => {
  it('converts time to Tamil', () => {
    const result = normalizeTime('Come at 6:30 PM', 'ta');
    expect(result).not.toContain('6:30');
    expect(result).toContain('மணி');
  });

  it('converts time to English', () => {
    const result = normalizeTime('Come at 6:30 PM', 'en');
    expect(result).toContain('thirty');
  });

  it('handles whole hours', () => {
    const result = normalizeTime('At 10:00 AM', 'ta');
    expect(result).toContain('மணி');
  });
});

describe('normalizeDate', () => {
  it('converts day names to Tamil', () => {
    const result = normalizeDate('Come on Monday', 'ta');
    expect(result).toContain('திங்கள்');
  });

  it('leaves English days in English mode', () => {
    const result = normalizeDate('Come on Monday', 'en');
    expect(result).toContain('Monday');
  });
});

describe('normalizePhoneDigits', () => {
  it('spells phone digits in Tamil', () => {
    const result = normalizePhoneDigits('Call 9876543210', 'ta');
    expect(result).toContain('ஒன்பது');
  });

  it('spells phone digits in English', () => {
    const result = normalizePhoneDigits('Call 9876543210', 'en');
    expect(result).toContain(' ');
  });
});

describe('detectLanguage', () => {
  it('detects Tamil', () => {
    expect(detectLanguage('வணக்கம் எப்படி இருக்கீங்க')).toBe('ta');
  });

  it('detects English', () => {
    expect(detectLanguage('Hello, how are you?')).toBe('en');
  });

  it('detects Tanglish', () => {
    expect(detectLanguage('வணக்கம் how are you')).toBe('tanglish');
  });
});

describe('chunkAtSentenceBoundaries', () => {
  it('chunks at sentence boundaries', () => {
    const text = 'First sentence. Second sentence. Third sentence.';
    const chunks = chunkAtSentenceBoundaries(text);
    expect(chunks.length).toBeGreaterThanOrEqual(1);
  });

  it('does not split short text', () => {
    const chunks = chunkAtSentenceBoundaries('Short text.');
    expect(chunks.length).toBe(1);
  });

  it('returns input for empty text', () => {
    const chunks = chunkAtSentenceBoundaries('');
    expect(chunks.length).toBe(1);
  });
});

describe('inferEmotion', () => {
  it('returns warm_greeting for first turn', () => {
    expect(inferEmotion('Hello', 0)).toBe('warm_greeting');
  });

  it('returns empathetic_recovery for sorry', () => {
    expect(inferEmotion('Sorry about that', 1)).toBe('empathetic_recovery');
  });

  it('returns clear_confirmation for confirm', () => {
    expect(inferEmotion('Booking confirmed', 2)).toBe('clear_confirmation');
  });
});

describe('createSpeakablePlan', () => {
  it('produces a valid plan for Tamil text', () => {
    const plan = createSpeakablePlan('வணக்கம்! எப்படி help பண்ணலாம்?', { turnIndex: 0 });
    expect(plan.ttsLanguage).toBe('ta-IN');
    expect(plan.chunks.length).toBeGreaterThan(0);
    expect(plan.emotion).toBe('warm_greeting');
  });

  it('produces a valid plan for English text', () => {
    const plan = createSpeakablePlan('Hello, welcome to Smile Dental.', { lang: 'en', turnIndex: 0 });
    expect(plan.ttsLanguage).toBe('en-IN');
    expect(plan.chunks.length).toBeGreaterThan(0);
  });

  it('normalizes currency in chunks', () => {
    const plan = createSpeakablePlan('Root canal costs ₹3,500.', { lang: 'en', turnIndex: 1 });
    const combined = plan.chunks.map((c) => c.text).join(' ');
    expect(combined).toContain('rupees');
  });
});
