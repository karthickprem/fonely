class VoiceCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.bufferSize = 2048;
    this.buffer = new Float32Array(this.bufferSize);
    this.bufferIndex = 0;
    this.active = true;
    this.vadThreshold = 0.01;
    this.speechFrames = 0;
    this.silenceFrames = 0;

    this.port.onmessage = (e) => {
      if (e.data.type === 'stop') this.active = false;
      if (e.data.type === 'start') this.active = true;
      if (e.data.type === 'set_threshold') this.vadThreshold = e.data.value;
    };
  }

  process(inputs) {
    if (!this.active) return true;

    const input = inputs[0];
    if (!input || !input[0]) return true;

    const channelData = input[0];
    let energy = 0;

    for (let i = 0; i < channelData.length; i++) {
      this.buffer[this.bufferIndex++] = channelData[i];
      energy += channelData[i] * channelData[i];

      if (this.bufferIndex >= this.bufferSize) {
        const pcm16 = float32ToPCM16(this.buffer);
        this.port.postMessage({
          type: 'audio',
          pcm: pcm16.buffer,
          samples: this.bufferSize,
        }, [pcm16.buffer]);
        this.buffer = new Float32Array(this.bufferSize);
        this.bufferIndex = 0;
      }
    }

    const rms = Math.sqrt(energy / channelData.length);
    const isSpeech = rms > this.vadThreshold;

    if (isSpeech) {
      this.speechFrames++;
      this.silenceFrames = 0;
    } else {
      this.silenceFrames++;
      this.speechFrames = 0;
    }

    // Report VAD state changes
    if (this.speechFrames === 3) {
      this.port.postMessage({ type: 'vad', active: true, rms });
    } else if (this.silenceFrames === 8) {
      this.port.postMessage({ type: 'vad', active: false, rms });
    }

    // Always send RMS for waveform visualization
    this.port.postMessage({ type: 'rms', value: rms });

    return true;
  }
}

function float32ToPCM16(float32Array) {
  const pcm16 = new Int16Array(float32Array.length);
  for (let i = 0; i < float32Array.length; i++) {
    const s = Math.max(-1, Math.min(1, float32Array[i]));
    pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
  }
  return pcm16;
}

registerProcessor('voice-capture-processor', VoiceCaptureProcessor);
