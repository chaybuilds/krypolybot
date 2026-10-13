import { describe, expect, it } from 'vitest';
import { cinePose, easeInOut, type OrbitPose } from '../src/hub/cine';

const wide: OrbitPose = { tx: 0, ty: 0, tz: 0, yaw: 0, pitch: 0.4, dist: 20, fov: 40 };
const subj: OrbitPose = { tx: 5, ty: 1, tz: -2, yaw: 0.5, pitch: 0.3, dist: 4, fov: 40 };

describe('trailer camera moves', () => {
  it('eases in and out', () => {
    expect(easeInOut(0)).toBe(0);
    expect(easeInOut(1)).toBe(1);
    expect(easeInOut(0.5)).toBeCloseTo(0.5);
    expect(easeInOut(-1)).toBe(0);
    expect(easeInOut(2)).toBe(1);
  });

  it('a push starts on the stage and ends on the subject', () => {
    const a = cinePose('push', 0, wide, subj, 0);
    const b = cinePose('push', 1, wide, subj, 0);
    expect(a.tx).toBeCloseTo(wide.tx);
    expect(a.dist).toBeCloseTo(wide.dist);
    expect(b.tx).toBeCloseTo(subj.tx);
    expect(b.dist).toBeCloseTo(subj.dist);
  });

  it('a reveal is a push in reverse, ending wider than the stage', () => {
    const a = cinePose('reveal', 0, wide, subj, 0);
    const b = cinePose('reveal', 1, wide, subj, 0);
    expect(a.tx).toBeCloseTo(subj.tx);
    expect(b.dist).toBeGreaterThan(wide.dist);
  });

  it('a close-up is nearer the subject than an orbit, on a longer lens', () => {
    const close = cinePose('close', 0.5, wide, subj, 0);
    const orbit = cinePose('orbit', 0.5, wide, subj, 0);
    expect(close.dist).toBeLessThan(subj.dist);
    expect(orbit.dist).toBeGreaterThan(subj.dist);
    expect(close.fov).toBeLessThan(subj.fov);
  });

  it('an orbit turns round the subject, the seed picking the direction', () => {
    const a0 = cinePose('orbit', 0, wide, subj, 0);
    const a1 = cinePose('orbit', 1, wide, subj, 0);
    const b1 = cinePose('orbit', 1, wide, subj, 1);
    expect(a1.yaw - a0.yaw).toBeGreaterThan(0.5);
    expect(Math.sign(b1.yaw - subj.yaw)).toBe(-Math.sign(a1.yaw - subj.yaw));
  });

  it('a vertical reel keeps the subject further off', () => {
    const land = cinePose('close', 0.5, wide, subj, 0, 16 / 9);
    const reel = cinePose('close', 0.5, wide, subj, 0, 9 / 16);
    expect(reel.dist).toBeGreaterThan(land.dist * 1.2);
  });
});
