class PlaybackMeterProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.threshold = 0.002;
    this.silenceFramesRequired = Math.ceil(sampleRate * 0.05 / 128);
    this.interrupted = false;
    this.framesSilent = 0;
    this.lastSignalFrame = null;
    this.totalFrames = 0;
    this.port.onmessage = ({data}) => {
      if (data?.type === 'interrupt') {
        this.interrupted = true;
        this.framesSilent = 0;
        this.lastSignalFrame = this.totalFrames;
      }
    };
  }

  process(inputs, outputs) {
    const input = inputs[0]?.[0];
    const output = outputs[0]?.[0];
    if (input && output) output.set(input);
    if (!input) return true;
    let sum = 0;
    for (const sample of input) sum += sample * sample;
    const rms = Math.sqrt(sum / Math.max(1, input.length));
    this.totalFrames++;
    if (this.interrupted) {
      if (rms > this.threshold) {
        this.lastSignalFrame = this.totalFrames;
        this.framesSilent = 0;
      } else {
        this.framesSilent++;
        if (this.framesSilent >= this.silenceFramesRequired) {
          const frameMs = 128 / sampleRate * 1000;
          this.port.postMessage({
            type: 'old-audio-stopped',
            lastSignalContextMs: (this.lastSignalFrame ?? this.totalFrames) * frameMs,
            thresholdRms: this.threshold,
            silenceHoldMs: this.silenceFramesRequired * frameMs,
            sampleRate,
            renderQuantum: 128,
          });
          this.interrupted = false;
        }
      }
    }
    return true;
  }
}
registerProcessor('playback-meter', PlaybackMeterProcessor);
