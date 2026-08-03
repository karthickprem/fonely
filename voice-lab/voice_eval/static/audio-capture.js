export function resampleTo16k(input, sourceRate) {
  if (sourceRate === 16000) return new Float32Array(input);
  const targetRate = 16000;
  const outputLength = Math.round(input.length * targetRate / sourceRate);
  const output = new Float32Array(outputLength);
  const cutoff = Math.min(1, targetRate / sourceRate) * 0.9;
  const radius = 12;
  for (let i = 0; i < outputLength; i++) {
    const center = i * sourceRate / targetRate;
    const left = Math.max(0, Math.floor(center) - radius);
    const right = Math.min(input.length - 1, Math.floor(center) + radius);
    let sum = 0, weightSum = 0;
    for (let j = left; j <= right; j++) {
      const x = (center - j) * cutoff;
      const sinc = x === 0 ? 1 : Math.sin(Math.PI * x) / (Math.PI * x);
      const distance = Math.abs(center - j) / (radius + 1);
      const window = distance <= 1 ? 0.5 * (1 + Math.cos(Math.PI * distance)) : 0;
      const weight = sinc * window * cutoff;
      sum += input[j] * weight;
      weightSum += weight;
    }
    output[i] = weightSum ? sum / weightSum : 0;
  }
  return output;
}

export function encodeWav16Mono(samples, sampleRate = 16000) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const text = (offset, value) => [...value].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
  text(0, 'RIFF'); view.setUint32(4, 36 + samples.length * 2, true); text(8, 'WAVE'); text(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  text(36, 'data'); view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const sample = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767), true);
  }
  return buffer;
}

export function inspectWav(buffer) {
  const view = new DataView(buffer);
  const ascii = (offset, length) => String.fromCharCode(...new Uint8Array(buffer, offset, length));
  if (buffer.byteLength < 44 || ascii(0, 4) !== 'RIFF' || ascii(8, 4) !== 'WAVE') throw new Error('Invalid WAV');
  return {format: view.getUint16(20, true), channels: view.getUint16(22, true), sampleRate: view.getUint32(24, true), bits: view.getUint16(34, true), dataBytes: view.getUint32(40, true), samples: view.getUint32(40, true) / 2};
}

export function analyzeAudio(samples, sampleRate = 16000) {
  let peak = 0, clipped = 0, speech = 0;
  const threshold = 0.015;
  for (const sample of samples) {
    const absolute = Math.abs(sample);
    peak = Math.max(peak, absolute);
    if (absolute >= 0.999) clipped += 1;
    if (absolute >= threshold) speech += 1;
  }
  const firstSpeech = samples.findIndex(sample => Math.abs(sample) >= threshold);
  let lastSpeech = -1;
  for (let i = samples.length - 1; i >= 0; i--) if (Math.abs(samples[i]) >= threshold) { lastSpeech = i; break; }
  return {peak, clippedRatio: samples.length ? clipped / samples.length : 0, speechRatio: samples.length ? speech / samples.length : 0, leadingSilenceMs: firstSpeech < 0 ? Math.round(samples.length / sampleRate * 1000) : Math.round(firstSpeech / sampleRate * 1000), trailingSilenceMs: lastSpeech < 0 ? Math.round(samples.length / sampleRate * 1000) : Math.round((samples.length - 1 - lastSpeech) / sampleRate * 1000), durationMs: Math.round(samples.length / sampleRate * 1000)};
}

export function combineChunks(chunks) {
  const length = chunks.reduce((total, chunk) => total + chunk.length, 0);
  const output = new Float32Array(length);
  let offset = 0;
  for (const chunk of chunks) { output.set(chunk, offset); offset += chunk.length; }
  return output;
}
