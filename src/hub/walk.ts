import * as THREE from 'three';

export type Seg = readonly [x0: number, z0: number, x1: number, z1: number];
export type Circle = readonly [x: number, z: number, r: number];
export type Platform =
  | { kind: 'rect'; x0: number; x1: number; z0: number; z1: number; h: number }
  | { kind: 'disc'; x: number; z: number; r: number; h: number };

export interface WalkSpace {
  walls: Seg[];
  props: Circle[];
  wallT: number;
  start: { x: number; z: number; yaw: number; pitch?: number };
  eye: number;
  speed: number;
  platforms?: Platform[];
  exits?: Seg[];
}

function segDist(x: number, z: number, [ax, az, bx, bz]: Seg): number {
  const dx = bx - ax, dz = bz - az;
  const len2 = dx * dx + dz * dz || 1e-9;
  const t = Math.max(0, Math.min(1, ((x - ax) * dx + (z - az) * dz) / len2));
  return Math.hypot(x - (ax + dx * t), z - (az + dz * t));
}

export function exitDistance(x: number, z: number, space: Pick<WalkSpace, 'exits'>): number {
  let best = Infinity;
  for (const s of space.exits ?? []) best = Math.min(best, segDist(x, z, s));
  return best;
}

export function atExit(x: number, z: number, space: Pick<WalkSpace, 'exits' | 'wallT'>): boolean {
  return exitDistance(x, z, space) <= BODY_R + space.wallT / 2 + 0.06;
}

export const BODY_R = 0.28;
export const STEP_UP = 0.12;

function onPlatform(p: Platform, x: number, z: number): boolean {
  return p.kind === 'rect' ? x >= p.x0 && x <= p.x1 && z >= p.z0 && z <= p.z1 : Math.hypot(x - p.x, z - p.z) <= p.r;
}

export function groundAt(x: number, z: number, space: Pick<WalkSpace, 'platforms'>, feet = 0): number {
  let g = 0;
  for (const p of space.platforms ?? []) if (p.h > g && p.h <= feet + STEP_UP && onPlatform(p, x, z)) g = p.h;
  return g;
}

export function collide(x: number, z: number, space: Pick<WalkSpace, 'walls' | 'props' | 'wallT' | 'platforms'>, r = BODY_R, feet = 0): [number, number] {
  for (let pass = 0; pass < 3; pass++) {
    let moved = false;
    for (const [ax, az, bx, bz] of space.walls) {
      const dx = bx - ax, dz = bz - az;
      const len2 = dx * dx + dz * dz || 1e-9;
      const t = Math.max(0, Math.min(1, ((x - ax) * dx + (z - az) * dz) / len2));
      const px = ax + dx * t, pz = az + dz * t;
      const ox = x - px, oz = z - pz;
      const d = Math.hypot(ox, oz);
      const min = r + space.wallT / 2;
      if (d < min) {
        const [nx, nz] = d > 1e-6 ? [ox / d, oz / d] : [-dz / Math.sqrt(len2), dx / Math.sqrt(len2)];
        x = px + nx * min;
        z = pz + nz * min;
        moved = true;
      }
    }
    for (const [cx, cz, cr] of space.props) {
      const ox = x - cx, oz = z - cz;
      const d = Math.hypot(ox, oz);
      const min = r + cr;
      if (d < min) {
        const [nx, nz] = d > 1e-6 ? [ox / d, oz / d] : [1, 0];
        x = cx + nx * min;
        z = cz + nz * min;
        moved = true;
      }
    }
    for (const p of space.platforms ?? []) {
      if (p.h <= feet + STEP_UP) continue;
      if (p.kind === 'disc') {
        const ox = x - p.x, oz = z - p.z;
        const d = Math.hypot(ox, oz);
        const min = r + p.r;
        if (d < min) {
          const [nx, nz] = d > 1e-6 ? [ox / d, oz / d] : [1, 0];
          x = p.x + nx * min;
          z = p.z + nz * min;
          moved = true;
        }
        continue;
      }
      const cx = Math.max(p.x0, Math.min(p.x1, x)), cz = Math.max(p.z0, Math.min(p.z1, z));
      const ox = x - cx, oz = z - cz;
      const d = Math.hypot(ox, oz);
      if (d >= r) continue;
      if (d > 1e-6) {
        x = cx + (ox / d) * r;
        z = cz + (oz / d) * r;
      } else {
        const out = [x - p.x0, p.x1 - x, z - p.z0, p.z1 - z];
        const k = out.indexOf(Math.min(...out));
        if (k === 0) x = p.x0 - r; else if (k === 1) x = p.x1 + r; else if (k === 2) z = p.z0 - r; else z = p.z1 + r;
      }
      moved = true;
    }
    if (!moved) break;
  }
  return [x, z];
}

export function step(x: number, z: number, dx: number, dz: number, space: Pick<WalkSpace, 'walls' | 'props' | 'wallT' | 'platforms'>, feet = 0): [number, number] {
  const dist = Math.hypot(dx, dz);
  const n = Math.max(1, Math.ceil(dist / (BODY_R * 0.5)));
  for (let i = 0; i < n; i++) [x, z] = collide(x + dx / n, z + dz / n, space, BODY_R, feet);
  return [x, z];
}

type Key = 'f' | 'b' | 'l' | 'r' | 'tl' | 'tr';
const MOVE_KEYS: Record<string, Key> = {
  KeyW: 'f', ArrowUp: 'f', KeyS: 'b', ArrowDown: 'b',
  KeyA: 'l', KeyD: 'r',
  ArrowLeft: 'tl', ArrowRight: 'tr',
};
const TURN_RATE = 1.1;
const JUMP_V = 3.8;
const GRAVITY = 9.8;

export class Walker {
  x = 0;
  z = 0;
  yaw = 0;
  pitch = 0;
  locked = false;
  fov = 68;
  exited = false;
  frozen = false;
  onKey: ((code: string, repeat: boolean) => boolean) | null = null;
  onKeyUp: ((code: string) => void) | null = null;
  onClick: ((clientX: number, clientY: number) => void) | null = null;
  onUse: ((down: boolean) => void) | null = null;
  onPointer: ((clientX: number, clientY: number) => void) | null = null;
  onWheel: ((dir: 1 | -1) => void) | null = null;
  drunk = 0;
  kick = 0;
  shake = 0;
  private clock = 0;
  private useDown = false;
  hop = 0;
  private vy = 0;
  private land = 0;
  onLand: (() => void) | null = null;
  private vx = 0;
  private vz = 0;
  private bob = 0;
  private keys = new Set<Key>();
  private run = false;
  private dragLook: { x: number; y: number; yaw: number; pitch: number } | null = null;

  constructor(public space: WalkSpace) {
    this.reset();
  }

  reset(): void {
    const s = this.space.start;
    [this.x, this.z] = collide(s.x, s.z, this.space);
    this.yaw = s.yaw;
    this.pitch = s.pitch ?? 0;
    this.vx = this.vz = 0;
    this.fov = 68;
    this.exited = false;
    this.kick = this.shake = 0;
    this.hop = this.vy = this.land = 0;
    this.keys.clear();
  }

  get bobPhase(): number {
    return this.bob;
  }

  jump(): boolean {
    if (this.frozen || this.airborne) return false;
    this.vy = JUMP_V;
    return true;
  }

  get airborne(): boolean {
    return this.vy !== 0 || this.hop > groundAt(this.x, this.z, this.space, this.hop) + 1e-4;
  }

  get moveFrac(): number {
    return Math.min(1, Math.hypot(this.vx, this.vz) / this.space.speed);
  }

  get exitDist(): number {
    return exitDistance(this.x, this.z, this.space);
  }

  private el: HTMLCanvasElement | null = null;

  relock(): void {
    const el = this.el;
    if (!el || document.pointerLockElement === el) return;
    try {
      const p = el.requestPointerLock() as unknown;
      if (p instanceof Promise) p.catch(() => { });
    } catch { }
  }

  attach(el: HTMLCanvasElement): () => void {
    this.el = el;
    const onKey = (down: boolean) => (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (e.key === 'Shift') this.run = down;
      if (!down) this.onKeyUp?.(e.code);
      if (down && this.onKey?.(e.code, e.repeat)) { e.preventDefault(); return; }
      if (e.code === 'Space') {
        e.preventDefault();
        if (down && !e.repeat) this.jump();
        return;
      }
      const k = MOVE_KEYS[e.code];
      if (!k) return;
      e.preventDefault();
      if (down) this.keys.add(k);
      else this.keys.delete(k);
    };
    const kd = onKey(true), ku = onKey(false);
    const blur = () => {
      this.keys.clear();
      this.run = false;
      if (this.useDown) { this.useDown = false; this.onUse?.(false); }
    };
    const look = (mx: number, my: number) => {
      if (this.frozen) return;
      this.yaw -= mx * 0.0022;
      this.pitch = THREE.MathUtils.clamp(this.pitch - my * 0.0022, -1.2, 1.2);
    };
    const move = (e: PointerEvent) => {
      if (document.pointerLockElement === el) { look(e.movementX, e.movementY); return; }
      if (this.frozen) { this.onPointer?.(e.clientX, e.clientY); return; }
      const d = this.dragLook;
      if (!d || this.frozen) return;
      this.yaw = d.yaw - (e.clientX - d.x) * 0.0035;
      this.pitch = THREE.MathUtils.clamp(d.pitch - (e.clientY - d.y) * 0.0035, -1.2, 1.2);
    };
    const down = (e: PointerEvent) => {
      if (e.button !== 0) return;
      if (document.pointerLockElement === el) {
        if (!this.frozen && this.onUse) { this.useDown = true; this.onUse(true); }
        return;
      }
      if (this.frozen) { this.onClick?.(e.clientX, e.clientY); return; }
      this.dragLook = { x: e.clientX, y: e.clientY, yaw: this.yaw, pitch: this.pitch };
      try {
        const p = el.requestPointerLock() as unknown;
        if (p instanceof Promise) p.catch(() => { });
      } catch { }
    };
    const up = () => {
      this.dragLook = null;
      if (this.useDown) { this.useDown = false; this.onUse?.(false); }
    };
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      if (this.frozen && this.onWheel) { this.onWheel(e.deltaY < 0 ? 1 : -1); return; }
      this.fov = THREE.MathUtils.clamp(this.fov * Math.exp(e.deltaY * 0.001), 35, 85);
    };
    const lockChange = () => { this.locked = document.pointerLockElement === el; };
    el.addEventListener('pointerdown', down);
    el.addEventListener('pointermove', move);
    el.addEventListener('wheel', wheel, { passive: false });
    window.addEventListener('pointerup', up);
    window.addEventListener('keydown', kd);
    window.addEventListener('keyup', ku);
    window.addEventListener('blur', blur);
    document.addEventListener('pointerlockchange', lockChange);
    return () => {
      el.removeEventListener('pointerdown', down);
      el.removeEventListener('pointermove', move);
      el.removeEventListener('wheel', wheel);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('keydown', kd);
      window.removeEventListener('keyup', ku);
      window.removeEventListener('blur', blur);
      document.removeEventListener('pointerlockchange', lockChange);
      if (document.pointerLockElement === el) document.exitPointerLock();
      this.locked = false;
      this.el = null;
      blur();
    };
  }

  update(dt: number): void {
    this.clock += dt;
    this.kick *= Math.exp(-12 * dt);
    this.shake = Math.max(0, this.shake - dt * 1.6);
    this.fall(dt);
    this.land = Math.max(0, this.land - dt * 5);
    if (this.frozen) {
      this.vx = this.vz = 0;
      return;
    }
    this.yaw += ((this.keys.has('tl') ? 1 : 0) - (this.keys.has('tr') ? 1 : 0)) * TURN_RATE * dt;
    const fwd = (this.keys.has('f') ? 1 : 0) - (this.keys.has('b') ? 1 : 0);
    const side = (this.keys.has('r') ? 1 : 0) - (this.keys.has('l') ? 1 : 0);
    const sy = Math.sin(this.yaw), cy = Math.cos(this.yaw);
    let wx = -sy * fwd + cy * side;
    let wz = -cy * fwd - sy * side;
    let len = Math.hypot(wx, wz);
    if (len > 0) { wx /= len; wz /= len; }
    if (len > 0 && this.drunk > 0) {
      const drift = this.drunk * Math.sin(this.clock * 0.7) * 0.55;
      wx += cy * drift;
      wz -= sy * drift;
      len = Math.hypot(wx, wz);
      wx /= len; wz /= len;
    }
    const speed = this.space.speed * (this.run ? 2 : 1);
    const k = 1 - Math.exp(-10 * dt);
    this.vx += (wx * speed - this.vx) * k;
    this.vz += (wz * speed - this.vz) * k;
    [this.x, this.z] = step(this.x, this.z, this.vx * dt, this.vz * dt, this.space, this.hop);
    if (len > 0 && atExit(this.x, this.z, this.space)) this.exited = true;
    const v = Math.hypot(this.vx, this.vz);
    this.bob += dt * v * 2.1;
  }

  private fall(dt: number): void {
    const ground = groundAt(this.x, this.z, this.space, this.hop);
    if (this.vy === 0 && this.hop <= ground + 1e-4) {
      this.hop = ground;
      return;
    }
    this.vy -= GRAVITY * dt;
    this.hop += this.vy * dt;
    const under = groundAt(this.x, this.z, this.space, Math.max(this.hop, ground));
    if (this.vy < 0 && this.hop <= under) {
      const hard = this.vy < -1.2;
      this.hop = under;
      this.vy = 0;
      if (hard) {
        this.land = 1;
        this.onLand?.();
      }
    }
  }

  apply(cam: THREE.PerspectiveCamera): void {
    const v = Math.min(1, Math.hypot(this.vx, this.vz) / this.space.speed);
    const bobY = Math.sin(this.bob * 2) * 0.035 * v;
    const bobX = Math.cos(this.bob) * 0.02 * v;
    const dip = Math.sin(this.land * Math.PI) * 0.07;
    cam.position.set(this.x + Math.cos(this.yaw) * bobX, this.space.eye + bobY + this.hop - dip, this.z - Math.sin(this.yaw) * bobX);
    const d = this.drunk, c = this.clock;
    const roll = d * (Math.sin(c * 0.8) * 0.07 + Math.sin(c * 1.9) * 0.02);
    const sh = this.shake * this.shake * 0.02;
    const jx = sh ? (Math.random() - 0.5) * sh : 0, jy = sh ? (Math.random() - 0.5) * sh : 0;
    cam.rotation.set(
      this.pitch + this.kick + d * Math.sin(c * 0.7 + 1) * 0.03 + jx,
      this.yaw + d * Math.sin(c * 0.55) * 0.05 + jy,
      roll, 'YXZ',
    );
    cam.updateMatrixWorld();
  }
}

export function visibleFrom(cam: THREE.Camera, p: THREE.Vector3, maxDist: number): boolean {
  const toP = p.clone().sub(cam.position);
  if (toP.lengthSq() > maxDist * maxDist) return false;
  const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(cam.quaternion);
  return toP.dot(fwd) > 0.3;
}
