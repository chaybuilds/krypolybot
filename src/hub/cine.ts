export type CineKind = 'establish' | 'push' | 'orbit' | 'close' | 'reveal';

export interface CineShot {
  kind: CineKind;
  dur: number;
  seed: number;
}

export interface OrbitPose {
  tx: number;
  ty: number;
  tz: number;
  yaw: number;
  pitch: number;
  dist: number;
  fov: number;
}

export function easeInOut(u: number): number {
  const x = Math.min(1, Math.max(0, u));
  return x * x * (3 - 2 * x);
}

function mix(a: number, b: number, k: number): number {
  return a + (b - a) * k;
}

function mixPose(a: OrbitPose, b: OrbitPose, k: number): OrbitPose {
  return {
    tx: mix(a.tx, b.tx, k), ty: mix(a.ty, b.ty, k), tz: mix(a.tz, b.tz, k),
    yaw: mix(a.yaw, a.yaw + wrapPi(b.yaw - a.yaw), k),
    pitch: mix(a.pitch, b.pitch, k), dist: mix(a.dist, b.dist, k), fov: mix(a.fov, b.fov, k),
  };
}

function wrapPi(a: number): number {
  return Math.atan2(Math.sin(a), Math.cos(a));
}

function side(seed: number): number {
  return Math.floor(seed) % 2 === 0 ? 1 : -1;
}

export function cinePose(kind: CineKind, u: number, wide: OrbitPose, subjectIn: OrbitPose, seed = 0, aspect = 16 / 9): OrbitPose {
  const subject = aspect < 1 ? { ...subjectIn, dist: subjectIn.dist * Math.sqrt(1 / aspect) } : subjectIn;
  const k = easeInOut(u);
  const s = side(seed);
  switch (kind) {
    case 'establish':
      return { ...wide, yaw: wide.yaw + s * (0.42 * (k - 0.5)), pitch: wide.pitch + 0.1 * (1 - k), dist: wide.dist * mix(1.18, 1.0, k) };
    case 'push': {
      const p = mixPose(wide, subject, k);
      return { ...p, yaw: p.yaw + s * 0.18 * (1 - k) };
    }
    case 'orbit':
      return { ...subject, yaw: subject.yaw + s * (1.1 * (u - 0.5)), dist: subject.dist * 1.25, pitch: subject.pitch + 0.06 };
    case 'close':
      return {
        ...subject,
        yaw: subject.yaw + s * 0.14 * (k - 0.5),
        dist: subject.dist * mix(0.82, 0.68, k),
        fov: Math.max(22, subject.fov * 0.78),
      };
    case 'reveal': {
      const p = mixPose(subject, wide, k);
      return { ...p, pitch: p.pitch + 0.12 * k, dist: p.dist * mix(1.0, 1.12, k) };
    }
  }
}
