import { expect, test } from "bun:test";
import { SILENCE_RMS, SignalMonitor, computeRms, inputDeviceLabel } from "./mic-signal.js";

test("computeRms is zero for silence and ~amplitude for a constant tone", () => {
  expect(computeRms(new Float32Array(480))).toBe(0);
  expect(computeRms([])).toBe(0);
  // A constant +0.5 block has RMS 0.5.
  expect(computeRms(new Float32Array(100).fill(0.5))).toBeCloseTo(0.5, 6);
});

test("computeRms of a sine wave is amplitude/sqrt(2)", () => {
  const n = 2400;
  const s = new Float32Array(n);
  for (let i = 0; i < n; i++) s[i] = Math.sin((2 * Math.PI * 440 * i) / 24000);
  expect(computeRms(s)).toBeCloseTo(1 / Math.SQRT2, 2);
});

test("SignalMonitor reports 'signal' immediately on audible input", () => {
  const m = new SignalMonitor();
  expect(m.status).toBe("pending");
  expect(m.push(0.2)).toBe("signal");
  expect(m.everHeard).toBe(true);
});

test("SignalMonitor warns only after SUSTAINED quiet, not a brief pause", () => {
  let t = 0;
  const m = new SignalMonitor({ quietMsBeforeWarn: 4000, now: () => t });
  // 3s of quiet — not long enough to warn yet.
  t = 3000;
  expect(m.push(0.0)).toBe("pending");
  // A word arrives → signal, resets the quiet clock.
  t = 3100;
  expect(m.push(0.1)).toBe("signal");
  // Brief 1s pause after speech — must NOT flap to no-signal.
  t = 4100;
  expect(m.push(0.0)).toBe("signal");
  // Now sustained quiet beyond the window → no-signal.
  t = 8200;
  expect(m.push(0.0)).toBe("no-signal");
});

test("SignalMonitor with all-silent input eventually warns (the reported bug)", () => {
  let t = 0;
  const m = new SignalMonitor({ quietMsBeforeWarn: 4000, now: () => t });
  // Simulate the observed failure: every block near-zero RMS.
  for (t = 0; t <= 5000; t += 500) m.push(1e-5);
  expect(m.status).toBe("no-signal");
  expect(m.everHeard).toBe(false);
});

test("threshold: a block just under SILENCE_RMS is quiet, just over is signal", () => {
  let t = 0;
  const m = new SignalMonitor({ now: () => t });
  expect(m.push(SILENCE_RMS - 1e-6)).not.toBe("signal");
  expect(m.push(SILENCE_RMS + 1e-6)).toBe("signal");
});

test("inputDeviceLabel returns the track label and never a raw id", () => {
  expect(inputDeviceLabel({ label: "MacBook Pro Microphone" })).toBe("MacBook Pro Microphone");
  expect(inputDeviceLabel({ label: "" })).toBe("Default microphone");
  expect(inputDeviceLabel(null)).toBe("Default microphone");
});
