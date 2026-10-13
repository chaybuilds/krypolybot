import type { Cocktail } from './bar';

export type ItemId = 'hand' | 'cig' | 'beer' | 'vodka' | 'gun' | 'cocktail';
export type Bottle = 'beer' | 'vodka';
export type Drink = Bottle | 'cocktail';

export const HOTBAR: readonly { id: ItemId; name: string; hint: string }[] = [
  { id: 'hand', name: 'Paws', hint: 'nothing in them' },
  { id: 'cig', name: 'Cigarette', hint: 'hold to drag · let go to exhale' },
  { id: 'beer', name: 'Beer', hint: 'hold to drink' },
  { id: 'vodka', name: 'Vodka', hint: 'hold to drink · it bites' },
  { id: 'gun', name: 'Revolver', hint: 'fire · R to reload' },
  { id: 'cocktail', name: 'Cocktail', hint: 'hold to drink · order one at the club bar' },
];

export const CYLINDER = 6;
export const RELOAD_S = 1.5;
const SWAP_S = 0.16;
const SIP: Record<Drink, number> = { beer: 0.11, vodka: 0.09, cocktail: 0.12 };
const BUZZ: Record<Bottle, number> = { beer: 0.032, vodka: 0.085 };
const SOBER_PER_S = 0.0165;
const REFILL_S = 1.8;
const RELIGHT_S = 1.3;
const DRAG_BURN = 0.09;
const IDLE_BURN = 0.004;
const BUTT = 0.2;
const MIN_DRAG = 0.25;
const COOLDOWN_S = 0.32;
const BLACK_S = 3.2;
const WAKE_AT = 1.6;

export type HotbarEvent =
  | { k: 'shot' }
  | { k: 'dry' }
  | { k: 'reloaded' }
  | { k: 'exhale'; strength: number }
  | { k: 'flick' }
  | { k: 'lit' }
  | { k: 'empty'; item: Drink }
  | { k: 'burn' }
  | { k: 'blackout' }
  | { k: 'wake' };

export function buzzWord(b: number): string {
  if (b < 0.12) return 'sober';
  if (b < 0.35) return 'tipsy';
  if (b < 0.6) return 'buzzed';
  if (b < 0.85) return 'hammered';
  return 'seeing double';
}

const isBottle = (i: ItemId): i is Bottle => i === 'beer' || i === 'vodka';
const isDrink = (i: ItemId): i is Drink => i === 'beer' || i === 'vodka' || i === 'cocktail';

export class Hotbar {
  slot = 0;
  shown: ItemId = 'hand';
  lower = 1;
  raise = 0;
  using = false;
  picked = 0;

  cig = 1;
  lit = true;
  private relight = 0;
  drawT = 0;
  exhale = 0;
  exhaleK = 0;

  level: Record<Drink, number> = { beer: 1, vodka: 1, cocktail: 0 };
  cocktail: Cocktail | null = null;
  private refill: Record<Bottle, number> = { beer: 0, vodka: 0 };
  private sipT = 0;

  ammo = CYLINDER;
  reload = 0;
  private cool = 0;
  recoil = 0;
  spin = 0;

  buzz = 0;
  black = 0;

  get item(): ItemId {
    return HOTBAR[this.slot].id;
  }

  giveCocktail(c: Cocktail): void {
    this.cocktail = c;
    this.level.cocktail = 1;
  }

  nameOf(i: number): string {
    const it = HOTBAR[i];
    return it.id === 'cocktail' && this.cocktail ? this.cocktail.name : it.name;
  }

  get ready(): boolean {
    return this.shown === this.item && this.lower < 0.05;
  }

  get reloadK(): number {
    return this.reload > 0 ? 1 - this.reload / RELOAD_S : 0;
  }

  get blackAlpha(): number {
    const b = this.black;
    if (b <= 0) return 0;
    if (b < 1.1) return b / 1.1;
    if (b < 1.9) return 1;
    return Math.max(0, 1 - (b - 1.9) / (BLACK_S - 1.9));
  }

  reset(): void {
    Object.assign(this, new Hotbar());
  }

  select(i: number): HotbarEvent[] {
    if (i < 0 || i >= HOTBAR.length || i === this.slot) return [];
    const ev = this.release();
    this.slot = i;
    this.picked = 0;
    return ev;
  }

  cycle(dir: 1 | -1): HotbarEvent[] {
    return this.select((this.slot + dir + HOTBAR.length) % HOTBAR.length);
  }

  press(): HotbarEvent[] {
    if (this.black > 0) return [];
    this.using = true;
    if (!this.ready || this.shown !== 'gun') return [];
    return this.fire();
  }

  release(): HotbarEvent[] {
    const ev: HotbarEvent[] = [];
    if (this.using) {
      if (this.shown === 'cig') this.endDrag(ev);
      if ((this.shown === 'vodka' || this.shown === 'cocktail') && this.sipT > 0.4) ev.push({ k: 'burn' });
    }
    this.using = false;
    this.drawT = 0;
    this.sipT = 0;
    return ev;
  }

  reloadGun(): boolean {
    if (this.shown !== 'gun' || !this.ready || this.reload > 0 || this.ammo >= CYLINDER) return false;
    this.reload = RELOAD_S;
    return true;
  }

  private fire(): HotbarEvent[] {
    if (this.reload > 0 || this.cool > 0) return [];
    if (this.ammo <= 0) {
      this.reloadGun();
      return [{ k: 'dry' }];
    }
    this.ammo--;
    this.cool = COOLDOWN_S;
    this.recoil = 1;
    this.spin++;
    return [{ k: 'shot' }];
  }

  private endDrag(ev: HotbarEvent[]): void {
    if (this.drawT >= MIN_DRAG) {
      const k = Math.min(1, this.drawT / 2);
      this.exhale = 0.5 + k * 1.1;
      this.exhaleK = k;
      ev.push({ k: 'exhale', strength: k });
    }
    this.drawT = 0;
  }

  update(dt: number): HotbarEvent[] {
    const ev: HotbarEvent[] = [];
    this.picked += dt;
    this.exhale = Math.max(0, this.exhale - dt);
    this.cool = Math.max(0, this.cool - dt);
    this.recoil = Math.max(0, this.recoil - dt * 7);

    const want = this.item;
    if (this.shown !== want) {
      this.lower = Math.min(1, this.lower + dt / SWAP_S);
      if (this.lower >= 1) {
        this.shown = want;
        this.raise = 0;
        this.reload = 0;
      }
    } else {
      const away = want === 'hand'
        || (isBottle(want) && this.refill[want] > 0)
        || (want === 'cocktail' && !this.cocktail)
        || (want === 'cig' && !this.lit);
      this.lower = away ? Math.min(1, this.lower + dt / SWAP_S) : Math.max(0, this.lower - dt / SWAP_S);
    }
    const ready = this.ready;

    if (this.relight > 0) {
      this.relight -= dt;
      if (this.relight <= 0) {
        this.relight = 0;
        this.cig = 1;
        this.lit = true;
        ev.push({ k: 'lit' });
      }
    }
    if (this.shown === 'cig' && this.lit) {
      const dragging = ready && this.using;
      if (dragging) this.drawT = Math.min(3, this.drawT + dt);
      this.cig -= dt * (dragging && this.raise > 0.6 ? DRAG_BURN : IDLE_BURN);
      if (this.cig <= BUTT) {
        this.endDrag(ev);
        this.cig = BUTT;
        this.lit = false;
        this.relight = RELIGHT_S;
        ev.push({ k: 'flick' });
      }
    }

    const up = ready && this.using && (
      this.shown === 'cig' ? this.lit : isDrink(this.shown) ? this.level[this.shown] > 0 : false
    );
    this.raise = up ? Math.min(1, this.raise + dt * 3.2) : Math.max(0, this.raise - dt * 4);

    let drinking = false;
    const b = this.shown;
    if (isDrink(b) && this.using && this.raise > 0.85 && this.level[b] > 0) {
      drinking = true;
      const sip = Math.min(this.level[b], SIP[b] * dt);
      this.level[b] -= sip;
      const perFull = b === 'cocktail' ? (this.cocktail?.strength ?? 0.3) : BUZZ[b] / SIP[b];
      this.buzz = Math.min(1, this.buzz + perFull * sip);
      this.sipT += dt;
      if (this.level[b] <= 1e-6) {
        this.level[b] = 0;
        if (b === 'cocktail') this.cocktail = null;
        else this.refill[b] = REFILL_S;
        if ((b === 'vodka' || b === 'cocktail') && this.sipT > 0.4) ev.push({ k: 'burn' });
        this.sipT = 0;
        ev.push({ k: 'empty', item: b });
      }
    }
    for (const k of ['beer', 'vodka'] as const) {
      if (this.refill[k] <= 0) continue;
      this.refill[k] -= dt;
      if (this.refill[k] <= 0) {
        this.refill[k] = 0;
        this.level[k] = 1;
      }
    }
    if (!drinking) this.buzz = Math.max(0, this.buzz - SOBER_PER_S * dt);

    if (this.reload > 0) {
      this.reload -= dt;
      if (this.reload <= 0) {
        this.reload = 0;
        this.ammo = CYLINDER;
        ev.push({ k: 'reloaded' });
      }
    } else if (this.shown === 'gun' && ready && this.ammo === 0 && this.cool <= 0) {
      this.reloadGun();
    }

    if (this.black > 0) {
      const before = this.black;
      this.black += dt;
      if (before < WAKE_AT && this.black >= WAKE_AT) {
        this.buzz = 0.35;
        this.slot = 0;
        ev.push({ k: 'wake' });
      }
      if (this.black >= BLACK_S) this.black = 0;
    } else if (this.buzz >= 1) {
      this.black = 1e-6;
      ev.push(...this.release());
      ev.push({ k: 'blackout' });
    }
    return ev;
  }
}
