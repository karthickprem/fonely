class FounderCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.recording = false;
    this.port.onmessage = ({data}) => {
      if (data.type === 'start') {
        this.recording = true;
        this.port.postMessage({type: 'started'});
      } else if (data.type === 'stop') {
        this.recording = false;
        this.port.postMessage({type: 'stopped'});
      }
    };
  }

  process(inputs) {
    const samples = inputs[0]?.[0];
    if (!samples) return true;
    let sum = 0, peak = 0, clipped = 0;
    for (const sample of samples) {
      const absolute = Math.abs(sample);
      sum += sample * sample;
      peak = Math.max(peak, absolute);
      if (absolute >= 0.999) clipped += 1;
    }
    this.port.postMessage({type: 'meter', rms: Math.sqrt(sum / samples.length), peak, clipped, count: samples.length});
    if (this.recording) {
      const copy = new Float32Array(samples);
      this.port.postMessage({type: 'samples', samples: copy.buffer}, [copy.buffer]);
    }
    return true;
  }
}
registerProcessor('founder-capture', FounderCaptureProcessor);
