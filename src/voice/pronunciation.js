const DENTAL_LEXICON = new Map([
  ['Smile Dental Clinic', { ta: 'ஸ்மைல் டென்டல் கிளினிக்', en: 'Smile Dental Clinic' }],
  ['Dr. Priya', { ta: 'டாக்டர் பிரியா', en: 'Doctor Priya' }],
  ['Dr. Arjun', { ta: 'டாக்டர் அர்ஜுன்', en: 'Doctor Arjun' }],
  ['Aminjikarai', { ta: 'அமிஞ்சிக்கரை', en: 'Aminjikarai' }],
  ['Chennai', { ta: 'சென்னை', en: 'Chennai' }],
  ['root canal', { ta: 'ரூட் கெனால்', en: 'root canal' }],
  ['scaling', { ta: 'ஸ்கேலிங்', en: 'scaling' }],
  ['extraction', { ta: 'பல் எடுத்தல்', en: 'extraction' }],
  ['consultation', { ta: 'கன்சல்டேஷன்', en: 'consultation' }],
  ['orthodontics', { ta: 'ஆர்த்தடான்டிக்ஸ்', en: 'orthodontics' }],
  ['general', { ta: 'ஜெனரல்', en: 'general' }],
  ['appointment', { ta: 'அப்பாயின்ட்மெண்ட்', en: 'appointment' }],
  ['Fonely', { ta: 'ஃபோன்லி', en: 'Fonely' }],
]);

const LOCALITY_NAMES = new Map([
  ['Anna Nagar', { ta: 'அண்ணா நகர்', en: 'Anna Nagar' }],
  ['T. Nagar', { ta: 'தி. நகர்', en: 'T Nagar' }],
  ['Adyar', { ta: 'அடையார்', en: 'Adyar' }],
  ['Mylapore', { ta: 'மயிலாப்பூர்', en: 'Mylapore' }],
  ['Aminjikarai', { ta: 'அமிஞ்சிக்கரை', en: 'Aminjikarai' }],
  ['Kodambakkam', { ta: 'கோடம்பாக்கம்', en: 'Kodambakkam' }],
]);

export function lookupPronunciation(term, lang = 'ta') {
  const key = lang === 'ta' || lang === 'ta-IN' ? 'ta' : 'en';
  const dental = DENTAL_LEXICON.get(term);
  if (dental) return dental[key];
  const locality = LOCALITY_NAMES.get(term);
  if (locality) return locality[key];
  return term;
}

export function applyLexicon(text, lang = 'ta') {
  let result = text;
  for (const [term] of DENTAL_LEXICON) {
    if (result.includes(term)) {
      result = result.replaceAll(term, lookupPronunciation(term, lang));
    }
  }
  for (const [term] of LOCALITY_NAMES) {
    if (result.includes(term)) {
      result = result.replaceAll(term, lookupPronunciation(term, lang));
    }
  }
  return result;
}

export function getDentalLexicon() {
  return Object.fromEntries(DENTAL_LEXICON);
}

export function getLocalityLexicon() {
  return Object.fromEntries(LOCALITY_NAMES);
}
