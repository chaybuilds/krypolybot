import { cacheMeasureText } from '../text';
import * as THREE from 'three';
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';
import { ShaderPass } from 'three/examples/jsm/postprocessing/ShaderPass.js';
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js';
import { hudNumbers, type HubData, type HubScoreboard } from '../data';
import type { HubSceneEngine } from '../HubStage';
import { cinePose, type CineShot, type OrbitPose } from '../cine';
import { paydayLine, type PaydayEvent } from '../payday';
import type { ActivityRecord } from '../../state/activity';
import { aiLine } from '../engine/jobs';
import { makeGlowTexture, Sparks } from '../engine/items';
import { drawHud, type FeedLine, type Tone } from '../engine/overlay';
import { PokerTable, type TableHost } from './table';
import { Member, disposeMemberMaterials } from './members';
import { drawBetBadges, drawBubbles, drawCard, drawMood, drawPlates, drawShowdown, type BetBadge, type CBubble, type Plate } from './overlay';
import { MEMBER_IDS, PERSONAS, type BubbleStyle, type MemberId, type Speaker } from './roster';
import { PokerRoom, POKER_WALK, TABLE_Y, seatOf } from './room';
import { Walker, visibleFrom } from '../walk';
import {
  agentScoreView, assignSeats, cents, forecastersFrom, noteLiveForecast, plateText, recentTickers,
  seatValue, seatVote, tableMode, type Forecaster, type Seat,
} from './seats';
import { subject } from '../text';
import type { McpAgent } from '@shared/agents';
import { configuredAgents, guideMotto, hubAgentIdentity, tagLabel } from '../../utils/agents';

interface Bubble { who: Speaker; text: string; age: number; life: number; seed: number }

const MAX_BUBBLES = 5;

const GRADE = {
  uniforms: { tDiffuse: { value: null as THREE.Texture | null }, uTime: { value: 0 }, uRes: { value: new THREE.Vector2(1, 1) } },
  vertexShader:`varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
  fragmentShader:`
    uniform sampler2D tDiffuse; uniform float uTime; uniform vec2 uRes; varying vec2 vUv;
    float h(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
    void main() {
      vec4 c = texture2D(tDiffuse, vUv);
      vec2 d = vUv - vec2(0.5, 0.46);
      d.x *= uRes.x / uRes.y * 0.72;
      float v = smoothstep(0.92, 0.22, length(d));
      c.rgb *= mix(0.4, 1.0, v);
      c.rgb *= vec3(1.06, 0.99, 0.9);
      c.rgb += (h(vUv * uRes + fract(uTime) * 91.0) - 0.5) * 0.016;
      gl_FragColor = c;
    }`,
};

function damp(a: number, b: number, k: number, dt: number): number {
  return b + (a - b) * Math.exp(-k * dt);
}

function agentStyle(color: string): BubbleStyle {
  return { ...PERSONAS.quant.style, bg: '#0B1220', fg: '#F1F5F9', edge: color, upper: false };
}

const LIVE_KEEP_MS = 120_000;

const PERSONA_BET: Record<MemberId, number> = { greed: 10, panic: 2, quant: 5, fomo: 7, doubt: 3, risk: 4 };

const STACK_EVEN = 18;

export class PokerEngine implements HubSceneEngine {
  private out: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private W = 1280;
  private H = 720;
  private glCanvas = document.createElement('canvas');
  private renderer: THREE.WebGLRenderer;
  private composer: EffectComposer;
  private bloom: UnrealBloomPass;
  private grade: ShaderPass;
  private rt: THREE.WebGLRenderTarget;
  private scene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(34, 16 / 9, 0.1, 80);

  private room: PokerRoom;
  private members = {} as Record<MemberId, Member>;
  private table: PokerTable;
  private glowTex = makeGlowTexture();
  private sparks: Sparks;

  private bubbles: Bubble[] = [];
  private feed: FeedLine[] = [];
  private speaker: Speaker | null = null;
  private banner: { text: string; sub: string; t: number } | null = null;
  private showdown: { line: string; color: string; t: number } | null = null;
  private account: HubData['account'] = null;
  private stats: HubData['scannerStats'] = null;
  private lastPnl: number | null = null;
  private pnlPulse = 0;
  private handN = -1;
  private stackCd = 0;
  private stackKey = '';

  private board: HubScoreboard | null | undefined = undefined;
  private boardAt = 0;
  private choice: 'personas' | 'agents' | null = null;
  private roster = new Map<string, Forecaster>();
  private agents: McpAgent[] | null = null;
  private agentsKey = '';
  private agentsRaw: unknown = undefined;
  private live: { rec: ActivityRecord; at: number }[] = [];
  private titles = new Map<string, string>();
  private seats: Seat[] | null = null;
  private seatBy = new Map<MemberId, Seat>();
  private settledSeen = new Map<string, number>();
  private halos = {} as Record<MemberId, THREE.Mesh>;
  private haloGeo = new THREE.TorusGeometry(0.34, 0.035, 8, 40);

  private yaw = 0;
  private pitch = 0.4;
  private dist = 15;
  private target = new THREE.Vector3(0, 1.9, -1.0);
  private yawGoal = 0;
  private pitchGoal = 0.4;
  private distGoal = 15;
  private targetGoal = new THREE.Vector3(0, 1.9, -1.0);
  private userYaw = 0;
  private userPitch = 0;
  private userZoom = 1;
  private follow: MemberId | null = null;
  private tour = false;
  private cineShot: CineShot | null = null;
  private cineT0 = 0;
  private drag: { x: number; y: number; moved: boolean; yaw: number; pitch: number } | null = null;
  private detach: (() => void) | null = null;
  private shake = 0;
  private walker = new Walker(POKER_WALK);
  private walking = false;

  private raf = 0;
  private running = false;
  private last = 0;
  private time = 0;
  private frameErr = false;
  private fpsFrames = 0;
  private fpsAcc = 0;
  fps = 0;
  onTourChange: ((on: boolean) => void) | null = null;
  onFocusChange: ((label: string | null) => void) | null = null;
  onWalkChange: ((on: boolean) => void) | null = null;

  private _v = new THREE.Vector3();
  private _v2 = new THREE.Vector3();
  private _look = new THREE.Vector3();
  private _plates: Plate[] = [];

  constructor(canvas: HTMLCanvasElement, w: number, h: number) {
    this.out = canvas;
    const ctx = canvas.getContext('2d', { alpha: false });
    if (!ctx) throw new Error('2D canvas unavailable');
    this.ctx = cacheMeasureText(ctx);

    this.renderer = new THREE.WebGLRenderer({ canvas: this.glCanvas, antialias: false, alpha: false, stencil: false, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(1);
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.15;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;

    this.scene.background = new THREE.Color('#080605');
    this.scene.fog = new THREE.Fog('#080605', 17, 34);
    this.scene.add(new THREE.HemisphereLight('#7a5c46', '#140d08', 0.85));

    this.room = new PokerRoom();
    this.scene.add(this.room.group);
    MEMBER_IDS.forEach((id, i) => {
      const [x, z] = seatOf(i);
      const m = new Member(id, x, z);
      this.members[id] = m;
      this.scene.add(m.group);
      const halo = new THREE.Mesh(this.haloGeo, new THREE.MeshBasicMaterial({ color: '#ffffff', toneMapped: false }));
      halo.rotation.x = Math.PI / 2;
      halo.visible = false;
      this.halos[id] = halo;
      this.scene.add(halo);
    });

    this.sparks = new Sparks(this.glowTex, 240);
    this.scene.add(this.sparks.points);

    const host: TableHost = {
      members: this.members,
      room: this.room,
      say: (who, text, opts) => this.say(who, text, opts),
      log: (text, tone) => this.log(text, tone),
      speaking: (who) => { this.speaker = who; },
      coins: (n) => {
        this.room.addPot(n);
        const top = this.room.potTop(this._v);
        this.sparks.burst(top.x, top.y, top.z, '#FDE68A', 10, 1.2, 1.8);
      },
      showdown: (won, pnl, subj) => {
        this.showdown = { line: `${won ? 'WON' : 'LOST'} ${pnl} · ${subj}`, color: won ? '#4ADE80' : '#F87171', t: 0 };
      },
    };
    this.table = new PokerTable(host);

    this.rt = new THREE.WebGLRenderTarget(w, h, { type: THREE.HalfFloatType, samples: 4 });
    this.composer = new EffectComposer(this.renderer, this.rt);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloom = new UnrealBloomPass(new THREE.Vector2(w, h), 0.5, 0.5, 0.95);
    this.composer.addPass(this.bloom);
    this.grade = new ShaderPass(GRADE);
    this.composer.addPass(this.grade);
    this.composer.addPass(new OutputPass());

    this.setOutput(canvas, w, h, 'page');
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.last = performance.now();
    const loop = () => {
      if (!this.running) return;
      this.raf = requestAnimationFrame(loop);
      this.frame();
    };
    this.raf = requestAnimationFrame(loop);
  }

  stop(): void {
    this.running = false;
    cancelAnimationFrame(this.raf);
  }

  ingest(d: HubData): void {
    this.account = d.account;
    this.stats = d.scannerStats;
    this.table.ingest(d);
    let reseat = false;
    if (d.councilChoice !== undefined && d.councilChoice !== this.choice) {
      this.choice = d.councilChoice;
      reseat = true;
    }
    let rebuild = false;
    const rawAgents = d.config ? d.config.mcpAgents ?? 'none' : null;
    if (d.config !== undefined && rawAgents !== this.agentsRaw) {
      this.agentsRaw = rawAgents;
      const agents = configuredAgents(d.config);
      const key = agents ? agents.map((a) => [a.id, a.name, a.emoji, a.color, guideMotto(a.guide)].join('\u0001')).join('\u0002') : '';
      if (key !== this.agentsKey) {
        this.agents = agents;
        this.agentsKey = key;
        rebuild = this.board !== undefined;
      }
    }
    let freshBoard = false;
    if (d.scoreboard !== undefined && d.scoreboard !== this.board) {
      freshBoard = this.board !== undefined;
      this.board = d.scoreboard;
      this.boardAt = Date.now();
      for (const r of d.scoreboard?.recent ?? []) if (r.title && !this.titles.has(r.conditionId)) this.titles.set(r.conditionId, r.title);
      this.live = this.live.filter((l) => this.boardAt - l.at < LIVE_KEEP_MS);
      rebuild = true;
    }
    if (rebuild) {
      this.roster = forecastersFrom(this.board, this.agents);
      for (const l of this.live) noteLiveForecast(this.roster, l.rec.ev, l.at, this.agents);
      reseat = true;
    }
    if (reseat) this.reseat();
    if (rebuild) this.noteSettled(freshBoard);
    const pnl = hudNumbers(d.account, d.scannerStats).pnl;
    if (pnl !== null && this.lastPnl !== null) {
      const bank = d.account?.startBankrollUsd || 1000;
      this.pnlPulse = Math.max(this.pnlPulse, Math.min(1, Math.abs(pnl - this.lastPnl) / (bank * 0.01)));
    }
    this.lastPnl = pnl;
  }

  private noteSettled(fresh: boolean): void {
    for (const s of this.seats ?? []) {
      const key = s.f.who.seatId;
      const now = s.f.score?.nPaired ?? 0;
      const was = this.settledSeen.get(key);
      this.settledSeen.set(key, now);
      if (!fresh || was === undefined || now <= was) continue;
      const skill = s.f.score?.skill ?? null;
      const sk = skill === null ? '' : ` · skill ${skill > 0 ? '+' : skill < 0 ? '−' : ''}${Math.abs(skill * 100).toFixed(0)}%`;
      this.showdown = { line: `${s.label} · ${now - was} settled${sk}`, color: s.f.who.color, t: 0 };
      this.log(`🂡 showdown · ${s.label} · ${now - was} settled`, 'gold');
    }
  }

  activity(rec: ActivityRecord, _replay: boolean): void {
    const ev = rec.ev;
    if (ev.kind === 'agentCall' && ev.conditionId && ev.title) this.titles.set(ev.conditionId, ev.title);
    if (ev.kind === 'aiAnalysis') {
      this.table.noteAnalysis(ev, rec.at);
      this.log(`🤖 ${aiLine(ev)}`, 'whale');
      if (ev.title) this.titles.set(ev.conditionId, ev.title);
    }
    const hit = noteLiveForecast(this.roster, ev, rec.at, this.agents);
    if (!hit) return;
    this.live.push({ rec, at: rec.at });
    if (this.live.length > 60) this.live.shift();
    if (ev.kind === 'agentCall' && ev.fairCents !== null) {
      const who = hubAgentIdentity(ev, this.agents);
      this.log(`🎯 ${tagLabel(who)} · ${subject(hit.ticker, (ev.title ?? this.titles.get(hit.ticker)) ?? hit.ticker)} fair ${Math.round(ev.fairCents)}¢`, 'gold');
    }
    this.reseat();
    if (this.table.mode === 'agents') this.table.noteAgenda(hit.ticker, this.titles.get(hit.ticker) ?? null);
  }

  private reseat(): void {
    const seats = assignSeats(this.roster);
    const mode = tableMode(this.choice, seats.length);
    const was = this.table.mode;
    this.seats = mode === 'agents' ? seats : null;
    this.seatBy = new Map((this.seats ?? []).map((x) => [x.member, x]));
    this.table.setSeats(this.seats);
    this.room.lammers = !this.seats;
    for (const id of MEMBER_IDS) {
      const seat = this.seatBy.get(id);
      this.members[id].group.visible = mode === 'personas' || !!seat;
      const h = this.halos[id];
      h.visible = !!seat;
      if (seat) (h.material as THREE.MeshBasicMaterial).color.set(seat.f.who.color);
    }
    if (this.seats) {
      this.room.setScore(agentScoreView(this.seats));
      if (was !== 'agents') for (const t of recentTickers(this.seats)) this.table.noteAgenda(t, this.titles.get(t) ?? null);
    }
    this.stackKey = '';
    this.stackCd = 0;
    if (this.follow && !this.members[this.follow].group.visible) this.setFollow(null);
  }

  private updateStacks(): void {
    const totals: (number | null)[] = [];
    const inPlay: boolean[] = [];
    if (this.seats) {
      MEMBER_IDS.forEach((id) => {
        const seat = this.seatBy.get(id);
        const skill = seat?.f.score?.skill ?? null;
        inPlay.push(!!seat);
        totals.push(skill === null ? STACK_EVEN : Math.max(3, Math.min(48, Math.round(STACK_EVEN + skill * 60))));
      });
    } else {
      for (const r of this.table.scoreRows()) {
        inPlay.push(true);
        totals.push(r.n ? Math.max(3, Math.round(4 + (r.right / r.n) * 40)) : STACK_EVEN);
      }
    }
    const key = `${totals.join()}|${inPlay.join()}`;
    if (key === this.stackKey) return;
    this.stackKey = key;
    totals.forEach((n, i) => this.room.setStacks(i, n, inPlay[i]));
  }

  private sizeBets(): void {
    const h = this.table.hand;
    MEMBER_IDS.forEach((id, i) => {
      let n = PERSONA_BET[id];
      if (h?.agent) {
        const c = this.seatBy.get(id)?.f.calls.get(h.agent.ticker);
        n = c && c.mid !== null ? 3 + Math.min(7, Math.round(Math.abs(c.fair - c.mid) / 2)) : 3;
      }
      this.room.setBetSize(i, n);
    });
  }

  private nameOf(id: MemberId): string {
    return this.seatBy.get(id)?.label ?? PERSONAS[id].name;
  }

  payday(ev: PaydayEvent): void {
    this.banner = { text: 'PAYDAY!', sub: paydayLine(ev), t: 0 };
    this.table.payday();
    this.log(`💰 PAYDAY ${paydayLine(ev)}`, 'gold');
    this.room.rainChips();
    this.shake = 0.6;
  }

  setOutput(canvas: HTMLCanvasElement, w: number, h: number, _mode: 'page' | 'cinema'): void {
    w = Math.max(64, Math.round(w));
    h = Math.max(64, Math.round(h));
    if (canvas !== this.out) {
      const ctx = canvas.getContext('2d', { alpha: false });
      if (!ctx) throw new Error('2D canvas unavailable');
      this.out = canvas;
      this.ctx = cacheMeasureText(ctx);
    }
    if (canvas.width !== w) canvas.width = w;
    if (canvas.height !== h) canvas.height = h;
    this.W = w;
    this.H = h;
    this.renderer.setSize(w, h, false);
    this.composer.setSize(w, h);
    this.bloom.setSize(Math.round(w / 2), Math.round(h / 2));
    (this.grade.uniforms.uRes.value as THREE.Vector2).set(w, h);
    this.sparks.setPixelScale(this.ui());
    this.attachInput();
  }

  cine(shot: CineShot | null): void {
    this.cineShot = shot;
    this.cineT0 = this.time;
    if (shot) { this.setFollow(null); this.userYaw = 0; this.userPitch = 0; this.userZoom = 1; }
  }

  setTour(on: boolean): void {
    if (this.tour === on) return;
    if (on && this.walking) this.setWalk(false);
    this.tour = on;
    if (on) this.setFollow(null);
    this.onTourChange?.(on);
  }

  resetView(): void {
    this.userYaw = 0;
    this.userPitch = 0;
    this.userZoom = 1;
    this.setFollow(null);
  }

  setWalk(on: boolean): void {
    if (on === this.walking) return;
    this.walking = on;
    if (on) {
      if (this.tour) this.setTour(false);
      this.setFollow(null);
      this.walker.reset();
    }
    this.room.setOnFoot(on);
    this.attachInput();
    this.onWalkChange?.(on);
  }

  private setFollow(id: MemberId | null): void {
    this.follow = id;
    this.onFocusChange?.(id ? this.nameOf(id).toLowerCase() : null);
  }

  dispose(): void {
    this.running = false;
    cancelAnimationFrame(this.raf);
    this.detach?.();
    const geos = new Set<THREE.BufferGeometry>();
    const mats = new Set<THREE.Material>();
    this.scene.traverse((o) => {
      const m = o as THREE.Mesh;
      if (m.geometry) geos.add(m.geometry);
      const mat = m.material as THREE.Material | THREE.Material[] | undefined;
      if (Array.isArray(mat)) mat.forEach((x) => mats.add(x));
      else if (mat) mats.add(mat);
    });
    geos.forEach((g) => g.dispose());
    mats.forEach((m) => m.dispose());
    disposeMemberMaterials();
    this.haloGeo.dispose();
    for (const id of MEMBER_IDS) this.members[id].dispose();
    this.room.dispose();
    this.glowTex.dispose();
    this.bloom.dispose();
    this.rt.dispose();
    this.composer.dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    this.scene.clear();
  }

  private say(who: Speaker, text: string, opts: { life?: number; force?: boolean } = {}): boolean {
    const mine = this.bubbles.findIndex((b) => b.who === who);
    if (mine >= 0) {
      if (!opts.force && this.bubbles[mine].age < this.bubbles[mine].life * 0.6) return false;
      this.bubbles.splice(mine, 1);
    }
    if (this.bubbles.length >= MAX_BUBBLES) {
      if (!opts.force) return false;
      this.bubbles.sort((a, b) => b.age - a.age);
      this.bubbles.shift();
    }
    this.bubbles.push({ who, text, age: 0, life: opts.life ?? 2.8, seed: Math.random() * 100 });
    if (who !== 'dealer') this.room.feed(`${this.nameOf(who)}: ${text}`);
    return true;
  }

  private log(text: string, tone: Tone): void {
    this.feed.push({ text, tone, age: 0 });
    if (this.feed.length > 6) this.feed.shift();
  }

  private ui(): number {
    return Math.min(this.W / 1280, this.H / 720);
  }

  private headOf(id: MemberId, out: THREE.Vector3): THREE.Vector3 {
    return this.members[id].bubbleAnchor(out).add(this._v2.set(0, -0.55, 0));
  }

  private updateCamera(dt: number): void {
    if (this.walking) {
      this.walker.update(dt);
      if (this.walker.exited) {
        this.setWalk(false);
        this.updateCamera(dt);
        return;
      }
      this.room.setExitNear((2.6 - this.walker.exitDist) / 1.8);
      this.walker.apply(this.camera);
      if (this.shake > 0) this.camera.position.y += (Math.random() - 0.5) * this.shake * 0.06;
      this.camera.fov = this.walker.fov;
      this.camera.aspect = this.W / this.H;
      this.camera.updateProjectionMatrix();
      return;
    }
    if (this.cineShot) {
      const aspect = this.W / this.H;
      const fit = THREE.MathUtils.clamp(Math.min(
        THREE.MathUtils.radToDeg(2 * Math.atan(7.8 / 15 / aspect)),
        THREE.MathUtils.radToDeg(2 * Math.atan(4.0 / 15))), 24, 52);
      const wide: OrbitPose = { tx: 0, ty: 1.9, tz: -1.0, yaw: 0, pitch: 0.4, dist: 15, fov: fit };
      let subject: OrbitPose = { tx: 0, ty: 1.45, tz: -1.1, yaw: 0, pitch: 0.34, dist: 7.2, fov: fit };
      if (this.speaker && this.speaker !== 'dealer') {
        const h = this.headOf(this.speaker, this._v);
        subject = { tx: h.x, ty: h.y, tz: h.z, yaw: THREE.MathUtils.clamp(Math.atan2(h.x, 6) * 0.5, -0.35, 0.35), pitch: 0.3, dist: 7.4, fov: fit };
      }
      const u = (this.time - this.cineT0) / Math.max(0.1, this.cineShot.dur);
      const p = cinePose(this.cineShot.kind, u, wide, subject, this.cineShot.seed, aspect);
      this.target.set(p.tx, p.ty, p.tz);
      this.yaw = p.yaw; this.pitch = p.pitch; this.dist = p.dist;
      const cp = Math.cos(this.pitch);
      this.camera.position.set(
        this.target.x + Math.sin(this.yaw) * cp * this.dist,
        this.target.y + Math.sin(this.pitch) * this.dist,
        this.target.z + Math.cos(this.yaw) * cp * this.dist,
      );
      this.camera.lookAt(this.target);
      this.camera.fov = p.fov;
      this.camera.aspect = aspect;
      this.camera.updateProjectionMatrix();
      return;
    }
    const d = this.table.hand;
    let dist = 15;
    let pitch = 0.4;
    let yaw = 0;
    const target = this._v.set(0, 1.9, -1.0);
    if (d && (d.phase === 'argue' || d.phase === 'vote' || d.phase === 'verdict')) {
      dist = 15 - Math.min(1, d.t / 7) * 2.0;
    }
    if (this.tour && d) {
      if (d.phase === 'argue' && this.speaker && this.speaker !== 'dealer') {
        this.headOf(this.speaker, target);
        dist = 7.4;
        yaw = THREE.MathUtils.clamp(Math.atan2(target.x, 6) * 0.5, -0.35, 0.35);
        pitch = 0.3;
      } else if (d.phase === 'vote') {
        target.set(0, 1.1, -0.4);
        dist = 10.5;
        pitch = 0.62;
      } else if (d.phase === 'verdict') {
        target.set(0, 1.45, -1.1);
        dist = 7.2;
        pitch = 0.34;
      }
    }
    if (this.table.paydayT > 0 && (this.tour || !this.follow)) {
      target.set(0, 1.4, -0.3);
      dist = 9.5;
    }
    if (this.follow) {
      this.headOf(this.follow, target);
      dist = 7.2;
      yaw = THREE.MathUtils.clamp(Math.atan2(target.x, 6) * 0.5, -0.35, 0.35);
      pitch = 0.3;
    }
    this.targetGoal.copy(target);
    this.distGoal = dist * this.userZoom;
    this.yawGoal = yaw + this.userYaw;
    this.pitchGoal = THREE.MathUtils.clamp(pitch + this.userPitch, 0.08, 1.0);

    const k = this.tour ? 2.2 : 1.6;
    this.target.x = damp(this.target.x, this.targetGoal.x, k, dt);
    this.target.y = damp(this.target.y, this.targetGoal.y, k, dt);
    this.target.z = damp(this.target.z, this.targetGoal.z, k, dt);
    this.dist = damp(this.dist, this.distGoal, k, dt);
    this.yaw = damp(this.yaw, this.yawGoal, k, dt);
    this.pitch = damp(this.pitch, this.pitchGoal, k, dt);

    const t = this.time;
    const yawD = this.yaw + Math.sin(t * 0.17) * 0.025;
    const pitchD = this.pitch + Math.sin(t * 0.23) * 0.01;
    const cp = Math.cos(pitchD);
    this.camera.position.set(
      this.target.x + Math.sin(yawD) * cp * this.dist,
      this.target.y + Math.sin(pitchD) * this.dist,
      this.target.z + Math.cos(yawD) * cp * this.dist,
    );
    if (this.shake > 0) {
      this.camera.position.x += (Math.random() - 0.5) * this.shake * 0.15;
      this.camera.position.y += (Math.random() - 0.5) * this.shake * 0.15;
    }
    this.camera.lookAt(this.target);
    const aspect = this.W / this.H;
    const vfovW = THREE.MathUtils.radToDeg(2 * Math.atan(7.8 / 15 / aspect));
    const vfovH = THREE.MathUtils.radToDeg(2 * Math.atan(4.0 / 15));
    this.camera.fov = THREE.MathUtils.clamp(Math.min(vfovW, vfovH), 24, 52);
    this.camera.aspect = aspect;
    this.camera.updateProjectionMatrix();
  }

  private attachInput(): void {
    this.detach?.();
    const el = this.out;
    if (this.walking) {
      this.detach = this.walker.attach(el);
      return;
    }
    const down = (e: PointerEvent) => {
      if (e.button !== 0) return;
      this.drag = { x: e.clientX, y: e.clientY, moved: false, yaw: this.userYaw, pitch: this.userPitch };
      el.setPointerCapture(e.pointerId);
    };
    const move = (e: PointerEvent) => {
      const d = this.drag;
      if (!d) return;
      const dx = e.clientX - d.x, dy = e.clientY - d.y;
      if (!d.moved && Math.hypot(dx, dy) < 5) return;
      d.moved = true;
      this.userYaw = THREE.MathUtils.clamp(d.yaw - dx * 0.004, -0.75, 0.75);
      this.userPitch = THREE.MathUtils.clamp(d.pitch + dy * 0.003, -0.3, 0.45);
    };
    const up = (e: PointerEvent) => {
      const d = this.drag;
      this.drag = null;
      try { el.releasePointerCapture(e.pointerId); } catch { }
      if (!d || d.moved) return;
      const id = this.pick(e.clientX, e.clientY);
      if (this.tour && id) this.setTour(false);
      this.setFollow(id && id !== this.follow ? id : null);
    };
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      this.userZoom = THREE.MathUtils.clamp(this.userZoom * Math.exp(e.deltaY * 0.0012), 0.45, 1.35);
    };
    el.addEventListener('pointerdown', down);
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
    el.addEventListener('wheel', wheel, { passive: false });
    this.detach = () => {
      el.removeEventListener('pointerdown', down);
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
      el.removeEventListener('wheel', wheel);
    };
  }

  private pick(clientX: number, clientY: number): MemberId | null {
    const r = this.out.getBoundingClientRect();
    const px = ((clientX - r.left) / r.width) * this.W;
    const py = ((clientY - r.top) / r.height) * this.H;
    let best: MemberId | null = null;
    let bd = (90 * this.ui()) ** 2;
    for (const id of MEMBER_IDS) {
      if (!this.members[id].group.visible) continue;
      const s = this.project(this.headOf(id, this._v));
      const d = (s.x - px) ** 2 + (s.y - py) ** 2;
      if (d < bd) { bd = d; best = id; }
    }
    return best;
  }

  private frame(): void {
    const now = performance.now();
    const dt = Math.min(0.05, Math.max(0, (now - this.last) / 1000));
    this.last = now;
    this.fpsFrames++;
    this.fpsAcc += dt;
    if (this.fpsAcc >= 0.5) {
      this.fps = Math.round(this.fpsFrames / this.fpsAcc);
      this.fpsFrames = 0;
      this.fpsAcc = 0;
    }
    try {
      this.update(dt);
      this.render();
    } catch (err) {
      if (!this.frameErr) {
        this.frameErr = true;
        console.error('[poker] frame failed', err);
      }
    }
  }

  private update(dt: number): void {
    this.time += dt;
    const t = this.time;
    this.table.update(dt);
    const d = this.table.hand;
    if (d && d.n !== this.handN) {
      this.handN = d.n;
      this.sizeBets();
    }
    this.room.setCard(!!d && d.phase !== 'out', !!d && d.t > 0.7);
    this.stackCd -= dt;
    if (this.stackCd <= 0) {
      this.stackCd = 0.5;
      this.updateStacks();
    }
    const look = this.speaker === 'dealer' ? this._look.copy(this.room.dealerHead)
      : this.speaker ? this.members[this.speaker].bubbleAnchor(this._look)
        : this._look.set(0, 1.4, 0);
    for (const id of MEMBER_IDS) {
      const m = this.members[id];
      m.lookAt.copy(id === this.speaker ? this.camera.position : look);
      m.update(dt, t);
    }
    for (const id of MEMBER_IDS) {
      const h = this.halos[id];
      if (!h.visible) continue;
      this.members[id].bubbleAnchor(h.position);
      h.position.y -= 0.12;
      h.rotation.z = t * 0.8;
    }
    this.room.update(dt, t, this.camera);
    this.sparks.update(dt);
    if (this.room.tableShake.v > 0.8) this.shake = Math.max(this.shake, 0.25);
    this.shake = Math.max(0, this.shake - dt * 1.5);

    for (const b of this.bubbles) b.age += dt;
    this.bubbles = this.bubbles.filter((b) => b.age < b.life);
    for (const f of this.feed) f.age += dt;
    this.pnlPulse = Math.max(0, this.pnlPulse - dt * 2.5);
    if (this.banner) {
      this.banner.t += dt;
      if (this.banner.t > 3.4) this.banner = null;
    }
    if (this.showdown) {
      this.showdown.t += dt;
      if (this.showdown.t > 3.6) this.showdown = null;
    }
    this.grade.uniforms.uTime.value = t;
    this.updateCamera(dt);
  }

  private seen(p: THREE.Vector3, maxDist: number): boolean {
    return !this.walking || visibleFrom(this.camera, p, maxDist);
  }

  private project(p: THREE.Vector3): { x: number; y: number; ok: boolean } {
    this._v2.copy(p).project(this.camera);
    return { x: (this._v2.x * 0.5 + 0.5) * this.W, y: (-this._v2.y * 0.5 + 0.5) * this.H, ok: this._v2.z < 1 };
  }

  private render(): void {
    this.composer.render();
    const g = this.ctx;
    g.drawImage(this.glCanvas, 0, 0, this.W, this.H);
    const u = this.ui();
    const t = this.time;
    const d = this.table.hand;

    const plates = this._plates;
    plates.length = 0;
    const ticker = this.table.currentTicker;
    const betting = !!d && (d.phase === 'vote' || d.phase === 'verdict' || d.phase === 'out');
    MEMBER_IDS.forEach((id, i) => {
      if (!this.seen(this.room.placards[i], 9)) return;
      const s = this.project(this.room.placards[i]);
      if (!s.ok) return;
      const dim = !!this.follow && this.follow !== id;
      if (this.seats) {
        const seat = this.seatBy.get(id);
        if (!seat) return;
        plates.push({
          x: s.x, y: s.y + 4 * u, name: plateText(seat, ticker), color: seat.f.who.color,
          vote: betting ? seatVote(seat, ticker) : null, dim,
        });
        return;
      }
      const vote = d && d.shownVotes > i ? d.votes[id] : null;
      plates.push({ x: s.x, y: s.y + 4 * u, name: PERSONAS[id].name, color: PERSONAS[id].color, vote, dim });
    });
    if (this.seats) {
      const est = (p: Plate) => (p.name.length * 7 + 30 + (p.vote === null ? 0 : 34)) * u;
      const byX = [...plates].sort((a, b) => a.x - b.x);
      for (let i = 1; i < byX.length; i++) {
        const a = byX[i - 1], b = byX[i];
        if (Math.abs(a.y - b.y) < 22 * u && b.x - a.x < (est(a) + est(b)) / 2 + 6 * u) b.y = a.y + 25 * u;
      }
    }
    const dsAt = this._v.copy(this.room.dealerHead).add(this._v2.set(0, -1.62, 0));
    const ds = this.project(dsAt);
    if (ds.ok && this.seen(dsAt, 9)) plates.push({ x: ds.x, y: ds.y, name: PERSONAS.dealer.name, color: PERSONAS.dealer.color, vote: null, dim: false });
    drawPlates(g, plates, u);

    if (this.seats && betting && ticker) {
      const badges: BetBadge[] = [];
      MEMBER_IDS.forEach((id, i) => {
        const seat = this.seatBy.get(id);
        const v = seatValue(seat, ticker);
        const side = seatVote(seat, ticker);
        if (v === null || side === null || !seat) return;
        const at = this.room.betAnchors[i];
        if (!this.seen(at, 9)) return;
        const p = this.project(at);
        if (p.ok) badges.push({ x: p.x, y: p.y, text: cents(v), yes: side, color: seat.f.who.color });
      });
      drawBetBadges(g, badges, u);
    }

    const ms = this.project(this.room.moodAnchor);
    if (ms.ok && this.seen(this.room.moodAnchor, 9)) {
      const moodSeat = this.table.mood === 'dealer' ? undefined : this.seatBy.get(this.table.mood);
      if (this.seats) drawMood(g, ms.x, ms.y, moodSeat ? moodSeat.f.who.name.toUpperCase() : 'FORECASTS', moodSeat?.f.who.color ?? '#E6C77E', u, this.W);
      else drawMood(g, ms.x, ms.y, PERSONAS[this.table.mood].name, PERSONAS[this.table.mood].color, u, this.W);
    }

    if (d && this.seen(this._v.set(0, TABLE_Y + 1.2, 0), 14)) {
      const anchor = this.project(this.walking ? this._v.set(0, TABLE_Y + 2.3, 0) : this._v.set(0, 0.1, 3.4));
      const from = this.project(this.room.cardAnchor);
      const f = d.f;
      const ag = d.agent;
      const kicker = ag ? ag.kicker : `${f.cat ? f.cat.toUpperCase() : 'MARKET'}  ·  ${f.whale ? '🐋 WHALE' : '📈 MOMENTUM'}`;
      const facts = ag ? ag.facts : [
        f.whaleUsd !== null ? `🐋 ${f.whaleUsd >= 1000 ? `$${(f.whaleUsd / 1000).toFixed(f.whaleUsd >= 1e4 ? 0 : 1)}K` : `$${f.whaleUsd.toFixed(0)}`} whale` : null,
        f.edge !== null ? `edge ${f.edge.toFixed(1)}` : null,
        f.conf !== null ? `conf ${f.conf.toFixed(0)}%` : null,
      ].filter(Boolean).join('  ·  ');
      drawCard(g, {
        x: anchor.x, y: anchor.y, fromX: from.x, fromY: from.y, t: d.t, out: d.outT, item: d.n,
        kicker, title: ag ? subject(ag.ticker, f.title) : f.title, dir: f.dir,
        dirLine: ag ? ag.dirLine : f.price !== null ? `${f.dir} @ ${f.price}¢` : f.dir,
        facts,
        tally: d.phase === 'vote' || d.phase === 'verdict' || d.phase === 'out' ? { yes: d.yes, no: d.no, shown: d.shownVotes } : null,
        stamp: d.stampT !== null ? { approved: !!d.approved, text: d.stampText, sub: d.stampSub, t: d.stampT, color: ag?.stampColor } : null,
        color: d.approved === null ? '#C9A55A' : d.approved ? '#4ADE80' : '#F87171',
      }, u, this.W, this.H);
    }

    const views: CBubble[] = [];
    for (const b of this.bubbles) {
      const at = b.who === 'dealer' ? this.room.dealerHead : this.members[b.who].bubbleAnchor(this._v);
      if (!this.seen(at, 12)) continue;
      const p = this.project(at);
      const seat = b.who === 'dealer' ? undefined : this.seatBy.get(b.who);
      views.push({ x: p.x, y: p.y, text: b.text, style: seat ? agentStyle(seat.f.who.color) : PERSONAS[b.who].style, age: b.age, life: b.life, seed: b.seed });
    }
    drawBubbles(g, views, u * (this.follow || this.tour ? 1.15 : 1), this.W, this.H, t);

    if (this.showdown) drawShowdown(g, this.showdown.line, this.showdown.color, this.showdown.t, u, this.W, this.H);

    const n = hudNumbers(this.account, this.stats);
    drawHud(g, {
      W: this.W, H: this.H, u, portrait: false,
      pnl: n.pnl,
      roi: n.roi,
      winRate: n.winRate,
      settledToday: n.settledToday,
      signals: n.signals,
      wins: 0, losses: 0,
      pnlPulse: this.pnlPulse,
      feed: this.feed,
      banner: this.banner,
      poly: null,
      showBrand: true,
      brandSub: this.seats ? 'HIGH STAKES · YOUR AGENTS' : 'HIGH STAKES · krypt.cc',
      brandColors: ['#F6DC9A', '#C9A55A', '#8A6A2A'],
    });
  }
}
