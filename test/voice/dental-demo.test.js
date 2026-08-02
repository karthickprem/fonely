import { describe, it, expect } from 'bun:test';
import {
  checkSafetyRules,
  getGreeting,
  getClinicInfo,
  DEMO_CLINIC,
} from '../../src/voice/dental-demo.js';

describe('checkSafetyRules', () => {
  it('returns safe for normal booking queries', () => {
    expect(checkSafetyRules('I want to book an appointment').safe).toBe(true);
  });

  it('returns safe for fee queries', () => {
    expect(checkSafetyRules('How much does root canal cost?').safe).toBe(true);
  });

  it('detects emergency keywords', () => {
    const result = checkSafetyRules('I am bleeding heavily');
    expect(result.safe).toBe(false);
    expect(result.type).toBe('emergency');
  });

  it('detects Tamil emergency keywords', () => {
    const result = checkSafetyRules('ரத்தம் வருகிறது');
    expect(result.safe).toBe(false);
    expect(result.type).toBe('emergency');
  });

  it('detects medical advice requests', () => {
    const result = checkSafetyRules('What medicine should I take for pain?');
    expect(result.safe).toBe(false);
    expect(result.type).toBe('medical_advice');
  });

  it('detects diagnosis requests', () => {
    const result = checkSafetyRules('Is it cancer?');
    expect(result.safe).toBe(false);
    expect(result.type).toBe('medical_advice');
  });

  it('detects X-ray questions', () => {
    const result = checkSafetyRules('Can you look at my x-ray?');
    expect(result.safe).toBe(false);
    expect(result.type).toBe('medical_advice');
  });

  it('provides Tamil response for emergency', () => {
    const result = checkSafetyRules('bleeding');
    expect(result.responseTa).toBeTruthy();
    expect(result.response).toBeTruthy();
  });
});

describe('getGreeting', () => {
  it('returns a greeting with clinic name', () => {
    const greeting = getGreeting();
    expect(greeting).toContain('Smile Dental Clinic');
    expect(greeting).toContain('Fonely');
  });
});

describe('DEMO_CLINIC', () => {
  it('has required clinic data', () => {
    expect(DEMO_CLINIC.name).toBe('Smile Dental Clinic');
    expect(DEMO_CLINIC.doctors.length).toBeGreaterThanOrEqual(2);
    expect(DEMO_CLINIC.services.length).toBeGreaterThanOrEqual(4);
  });

  it('has synthetic appointment slots', () => {
    expect(DEMO_CLINIC.syntheticSlots.tomorrow.length).toBeGreaterThan(0);
    expect(DEMO_CLINIC.syntheticSlots.dayAfter.length).toBeGreaterThan(0);
  });

  it('has correct doctor specializations', () => {
    const priya = DEMO_CLINIC.doctors.find((d) => d.name === 'Dr. Priya');
    expect(priya).toBeTruthy();
    expect(priya.specializations).toContain('root canal');
    expect(priya.specializations).toContain('scaling');

    const arjun = DEMO_CLINIC.doctors.find((d) => d.name === 'Dr. Arjun');
    expect(arjun).toBeTruthy();
    expect(arjun.specializations).toContain('orthodontics');
  });

  it('has correct pricing', () => {
    const consultation = DEMO_CLINIC.services.find((s) => s.name === 'General Consultation');
    expect(consultation.price).toBe(300);
    expect(consultation.duration).toBe(20);

    const rootCanal = DEMO_CLINIC.services.find((s) => s.name === 'Root Canal');
    expect(rootCanal.priceMin).toBe(3500);
    expect(rootCanal.priceMax).toBe(5500);
  });
});

describe('getClinicInfo', () => {
  it('returns clinic info', () => {
    const info = getClinicInfo();
    expect(info.name).toBe('Smile Dental Clinic');
  });
});
