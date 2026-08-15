// Pure, testable mic-signal logic for the Fonely input meter.
//
// The server proves caller audio frames DO flow; the problem observed was that
// the selected input device delivered near-silence (all-zero PCM). This module
// turns a stream of RMS samples into a user-facing status so a silent mic is
// visible ("No microphone signal — check mute/input device") instead of a demo
// that mysteriously never responds. No audio is stored or transmitted here — it
// only reduces frames to a single RMS number and a status string.

/**
 * Root-mean-square of a block of PCM samples in [-1, 1] (Web Audio float range).
 * @param {Float32Array|number[]} samples
 * @returns {number} RMS in [0, 1]
 */
export function computeRms(samples) {
  const n = samples.length;
  if (!n) return 0;
  let sum = 0;
  for (let i = 0; i < n; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / n);
}

// Below this RMS a mic is treated as silent. Real speech sits well above; idle
// room noise is typically a few thousandths. 0.004 ≈ -48 dBFS.
export const SILENCE_RMS = 0.004;

/**
 * Tracks whether the mic has produced audible signal recently. Emits "signal"
 * as soon as any block exceeds the threshold, and "no-signal" only after a
 * SUSTAINED quiet period (so a natural pause between words does not flap the UI).
 */
export class SignalMonitor {
  /**
   * @param {object} [opts]
   * @param {number} [opts.silenceThreshold] RMS below which a block is "quiet".
   * @param {number} [opts.quietMsBeforeWarn] sustained quiet before "no-signal".
   * @param {() => number} [opts.now] injectable clock (ms) for tests.
   */
  constructor(opts = {}) {
    this._threshold = opts.silenceThreshold ?? SILENCE_RMS;
    this._quietMsBeforeWarn = opts.quietMsBeforeWarn ?? 4000;
    this._now = opts.now ?? (() => Date.now());
    this._everHeard = false;
    this._quietSince = this._now();
    this._status = "pending"; // "pending" | "signal" | "no-signal"
  }

  get status() {
    return this._status;
  }

  get everHeard() {
    return this._everHeard;
  }

  /**
   * Feed one RMS reading; returns the current status.
   * @param {number} rms
   * @returns {"pending"|"signal"|"no-signal"}
   */
  push(rms) {
    const t = this._now();
    if (rms >= this._threshold) {
      this._everHeard = true;
      this._quietSince = t;
      this._status = "signal";
      return this._status;
    }
    // Quiet block. Warn only once quiet has persisted long enough.
    if (t - this._quietSince >= this._quietMsBeforeWarn) {
      this._status = "no-signal";
    }
    return this._status;
  }
}

/**
 * A short, human-readable label for the selected input device, with device IDs
 * deliberately stripped (never surface raw deviceId strings).
 * @param {MediaStreamTrack} track
 * @returns {string}
 */
export function inputDeviceLabel(track) {
  const label = (track && typeof track.label === "string" && track.label.trim()) || "";
  return label || "Default microphone";
}
