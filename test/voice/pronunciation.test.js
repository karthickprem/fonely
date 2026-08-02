import { describe, it, expect } from 'bun:test';
import {
  lookupPronunciation,
  applyLexicon,
  getDentalLexicon,
  getLocalityLexicon,
} from '../../src/voice/pronunciation.js';

describe('lookupPronunciation', () => {
  it('returns Tamil pronunciation for dental terms', () => {
    expect(lookupPronunciation('Dr. Priya', 'ta')).toBe('டாக்டர் பிரியா');
  });

  it('returns English pronunciation for dental terms', () => {
    expect(lookupPronunciation('Dr. Priya', 'en')).toBe('Doctor Priya');
  });

  it('returns Tamil for root canal', () => {
    expect(lookupPronunciation('root canal', 'ta')).toBe('ரூட் கெனால்');
  });

  it('returns Tamil for locality names', () => {
    expect(lookupPronunciation('Aminjikarai', 'ta')).toBe('அமிஞ்சிக்கரை');
  });

  it('returns the term itself for unknown terms', () => {
    expect(lookupPronunciation('unknown term', 'ta')).toBe('unknown term');
  });
});

describe('applyLexicon', () => {
  it('replaces known terms in Tamil mode', () => {
    const result = applyLexicon('Come to Smile Dental Clinic for root canal.', 'ta');
    expect(result).toContain('ஸ்மைல் டென்டல் கிளினிக்');
    expect(result).toContain('ரூட் கெனால்');
  });

  it('replaces locality names', () => {
    const result = applyLexicon('Located in Aminjikarai', 'ta');
    expect(result).toContain('அமிஞ்சிக்கரை');
  });
});

describe('getDentalLexicon', () => {
  it('returns a non-empty lexicon', () => {
    const lexicon = getDentalLexicon();
    expect(Object.keys(lexicon).length).toBeGreaterThan(0);
    expect(lexicon['Smile Dental Clinic']).toBeTruthy();
  });
});

describe('getLocalityLexicon', () => {
  it('returns a non-empty locality lexicon', () => {
    const lexicon = getLocalityLexicon();
    expect(Object.keys(lexicon).length).toBeGreaterThan(0);
    expect(lexicon['Anna Nagar']).toBeTruthy();
  });
});
