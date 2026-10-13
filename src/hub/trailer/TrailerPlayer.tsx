import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { Circle, Clapperboard, Loader2, X } from 'lucide-react';
import type { HubViewId } from '../views';
import type { TrailerTarget } from './context';
import {
  FORMAT_SIZE, buildPlan, planDuration, stepAt, viewFor,
  type TrailerFormat, type TrailerLength, type TrailerStep,
} from './plan';

type Phase = 'setup' | 'playing' | 'saving';

const PIXEL = '"Press Start 2P", monospace';
const SANS = '"Chakra Petch", system-ui, sans-serif';

function pickMime(): { mime: string; ext: 'mp4' | 'webm' } | null {
  if (typeof MediaRecorder === 'undefined') return null;
  const tries: [string, 'mp4' | 'webm'][] = [
    ['video/mp4;codecs=avc1', 'mp4'], ['video/mp4', 'mp4'],
    ['video/webm;codecs=vp9', 'webm'], ['video/webm;codecs=vp8', 'webm'], ['video/webm', 'webm'],
  ];
  for (const [mime, ext] of tries) if (MediaRecorder.isTypeSupported(mime)) return { mime, ext };
  return null;
}

function fade(local: number, dur: number, edge = 0.35): number {
  return Math.max(0, Math.min(1, local / edge, (dur - local) / edge));
}

function drawCard(g: CanvasRenderingContext2D, w: number, h: number, s: Extract<TrailerStep, { kind: 'card' }>, local: number): void {
  const bg = g.createLinearGradient(0, 0, w, h);
  bg.addColorStop(0, '#04050c');
  bg.addColorStop(1, '#140b2e');
  g.fillStyle = bg;
  g.fillRect(0, 0, w, h);
  const a = fade(local, s.dur);
  const u = Math.min(1, local / s.dur);
  const portrait = h > w;
  const base = Math.min(w, h);
  g.save();
  g.globalAlpha = a;
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  const big = s.end || s.title === 'KRYPT POLYBOT';
  const titleSize = Math.round(base * (big ? (portrait ? 0.075 : 0.07) : (portrait ? 0.058 : 0.05)));
  g.font = big ? `400 ${titleSize}px ${PIXEL}` : `700 ${titleSize}px ${SANS}`;
  g.shadowColor = 'rgba(96,165,250,0.85)';
  g.shadowBlur = titleSize * 0.6;
  g.fillStyle = '#ffffff';
  const scale = 1 + 0.04 * u;
  g.translate(w / 2, h / 2 - (s.sub ? titleSize * 0.55 : 0));
  g.scale(scale, scale);
  wrapText(g, s.title, 0, 0, w * 0.86, titleSize * 1.25);
  g.restore();
  if (s.sub) {
    g.save();
    g.globalAlpha = a * Math.min(1, Math.max(0, (local - 0.25) / 0.4));
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    const subSize = Math.round(base * (portrait ? 0.036 : 0.032));
    g.font = `600 ${subSize}px ${SANS}`;
    g.fillStyle = '#bfdbfe';
    g.fillText(s.sub, w / 2, h / 2 + titleSize * 0.95);
    g.restore();
  }
}

function wrapText(g: CanvasRenderingContext2D, text: string, x: number, y: number, maxW: number, lh: number): void {
  const words = text.split(' ');
  const lines: string[] = [];
  let line = '';
  for (const wd of words) {
    const next = line ? `${line} ${wd}` : wd;
    if (line && g.measureText(next).width > maxW) { lines.push(line); line = wd; } else line = next;
  }
  if (line) lines.push(line);
  const y0 = y - ((lines.length - 1) * lh) / 2;
  lines.forEach((l, i) => g.fillText(l, x, y0 + i * lh));
}

function drawCaption(g: CanvasRenderingContext2D, w: number, h: number, text: string, local: number, dur: number): void {
  const a = fade(local - 0.3, dur - 0.3, 0.3);
  if (a <= 0) return;
  const portrait = h > w;
  const size = Math.round(Math.min(w, h) * (portrait ? 0.04 : 0.034));
  g.save();
  g.font = `700 ${size}px ${SANS}`;
  const tw = Math.min(g.measureText(text).width, w * 0.86);
  const padX = size * 0.9;
  const bh = size * 2.1;
  const bw = tw + padX * 2;
  const x = (w - bw) / 2;
  const y = h * (portrait ? 0.78 : 0.8) + (1 - a) * size * 0.6;
  g.globalAlpha = a;
  g.fillStyle = 'rgba(8,6,22,0.72)';
  g.strokeStyle = 'rgba(96,165,250,0.55)';
  g.lineWidth = Math.max(2, size * 0.06);
  g.beginPath();
  g.roundRect(x, y, bw, bh, bh / 2);
  g.fill();
  g.stroke();
  g.fillStyle = '#ffffff';
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.fillText(text, w / 2, y + bh / 2, w * 0.86);
  g.restore();
}

export function TrailerPlayer({ onTarget, onView, onClose }: {
  onTarget: (t: TrailerTarget | null) => void;
  onView: (v: HubViewId) => void;
  onClose: () => void;
}) {
  const [phase, setPhase] = useState<Phase>('setup');
  const [format, setFormat] = useState<TrailerFormat>('reel');
  const [length, setLength] = useState<TrailerLength>(30);
  const [record, setRecord] = useState(true);
  const [note, setNote] = useState<string | null>(null);
  const [progress, setProgress] = useState(0);
  const outRef = useRef<HTMLCanvasElement>(null);
  const raf = useRef(0);
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const takes = useRef(0);
  const closing = useRef(false);
  const mime = pickMime();

  const stopLoop = useCallback(() => {
    cancelAnimationFrame(raf.current);
    raf.current = 0;
  }, []);

  useEffect(() => () => {
    stopLoop();
    closing.current = true;
    try { recorder.current?.state === 'recording' && recorder.current.stop(); } catch { }
    onTarget(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const finish = useCallback(async (save: boolean) => {
    stopLoop();
    onTarget(null);
    const rec = recorder.current;
    recorder.current = null;
    if (!rec || rec.state === 'inactive') { setPhase('setup'); return; }
    if (!save) { try { rec.stop(); } catch { } chunks.current = []; setPhase('setup'); return; }
    setPhase('saving');
    const stopped = new Promise<void>((r) => { rec.onstop = () => r(); });
    try { rec.stop(); } catch { }
    await stopped;
    const ext = rec.mimeType.includes('mp4') ? 'mp4' : 'webm';
    const blob = new Blob(chunks.current, { type: rec.mimeType });
    chunks.current = [];
    try {
      const bytes = new Uint8Array(await blob.arrayBuffer());
      const name = `krypt-polybot-${format === 'reel' ? 'reel' : 'trailer'}-${length}s`;
      const res = await window.krypt.app.saveVideo(bytes, ext, name);
      if (res.ok) setNote(`Saved ${res.data ?? ''}`.trim());
      else if (res.message !== 'canceled') setNote(res.message ?? 'Could not save the recording.');
    } catch (e) {
      setNote(`Could not save the recording: ${e instanceof Error ? e.message : String(e)}`);
    }
    if (!closing.current) setPhase('setup');
  }, [format, length, onTarget, stopLoop]);

  const start = useCallback(() => {
    const out = outRef.current;
    if (!out) return;
    const { w, h } = FORMAT_SIZE[format];
    out.width = w;
    out.height = h;
    const scene = document.createElement('canvas');
    scene.width = w;
    scene.height = h;
    const g = out.getContext('2d');
    if (!g) return;
    const plan = buildPlan(length, takes.current++);
    const total = planDuration(plan);
    setNote(null);
    setProgress(0);
    setPhase('playing');

    let key = 0;
    let lastIdx = -1;
    let target: TrailerTarget = { canvas: scene, w, h, shot: null, shotKey: key };
    onTarget(target);
    const first = viewFor(plan, 0);
    if (first) onView(first);

    if (record && mime) {
      try {
        const stream = out.captureStream(30);
        const r = new MediaRecorder(stream, { mimeType: mime.mime, videoBitsPerSecond: 16_000_000 });
        chunks.current = [];
        r.ondataavailable = (e) => { if (e.data.size) chunks.current.push(e.data); };
        r.start(1000);
        recorder.current = r;
      } catch (e) {
        setNote(`Recording is unavailable here (${e instanceof Error ? e.message : String(e)}); playing without it.`);
      }
    }

    const t0 = performance.now();
    const frame = () => {
      const t = (performance.now() - t0) / 1000;
      const at = stepAt(plan, t);
      if (!at) {
        void finish(true);
        return;
      }
      if (at.index !== lastIdx) {
        lastIdx = at.index;
        const v = viewFor(plan, at.index);
        if (v) onView(v);
        if (at.step.kind === 'shot') {
          key += 1;
          target = { canvas: scene, w, h, shot: at.step.shot, shotKey: key };
          onTarget(target);
        }
        setProgress(Math.min(1, t / total));
      }
      const s = at.step;
      if (s.kind === 'card') {
        drawCard(g, w, h, s, at.local);
      } else {
        g.fillStyle = '#000';
        g.fillRect(0, 0, w, h);
        g.drawImage(scene, 0, 0, w, h);
        const prev = plan[at.index - 1];
        if (prev && prev.kind === 'card' && at.local < 0.35) {
          g.fillStyle = `rgba(0,0,0,${1 - at.local / 0.35})`;
          g.fillRect(0, 0, w, h);
        }
        if (s.caption) drawCaption(g, w, h, s.caption, at.local, s.dur);
      }
      raf.current = requestAnimationFrame(frame);
    };
    raf.current = requestAnimationFrame(frame);
  }, [finish, format, length, mime, onTarget, onView, record]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      e.preventDefault();
      if (phase === 'playing') void finish(false);
      else if (phase === 'setup') onClose();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [finish, onClose, phase]);

  const { w, h } = FORMAT_SIZE[format];
  const fit: CSSProperties = w / h > window.innerWidth / window.innerHeight
    ? { width: '100vw', height: `${(100 * h * window.innerWidth) / (w * window.innerHeight)}vh` }
    : { height: '100vh', width: `${(100 * w * window.innerHeight) / (h * window.innerWidth)}vw` };

  return createPortal(
    <div
      className="fixed inset-0 z-[80] grid place-items-center bg-black"
      style={{ WebkitAppRegion: 'no-drag', cursor: phase === 'playing' ? 'none' : 'default' } as CSSProperties}
      role="dialog"
      aria-label="Agent Hub trailer"
      data-testid="trailer"
    >
      <canvas ref={outRef} style={{ ...fit, maxWidth: '100vw', maxHeight: '100vh', visibility: phase === 'playing' ? 'visible' : 'hidden' }} />
      {phase === 'playing' && (
        <div className="pointer-events-none absolute bottom-0 left-0 h-[3px] bg-krypt-purple/80 transition-[width] duration-300" style={{ width: `${progress * 100}%` }} />
      )}
      {phase === 'saving' && (
        <div className="absolute inset-0 grid place-items-center bg-black/70 text-sm text-white">
          <span className="flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin" /> Saving the recording…</span>
        </div>
      )}
      {phase === 'setup' && (
        <div className="absolute inset-0 grid place-items-center p-6">
          <div className="w-full max-w-md rounded-2xl border border-white/10 bg-[#0c0d1a] p-5 text-white shadow-2xl">
            <div className="mb-4 flex items-center gap-2">
              <Clapperboard className="h-5 w-5 text-krypt-purple" />
              <h3 className="flex-1 text-base font-semibold">Trailer</h3>
              <button type="button" onClick={onClose} aria-label="Close" className="krypt-btn-ghost p-1"><X className="h-4 w-4" /></button>
            </div>
            <p className="mb-4 text-xs leading-relaxed text-krypt-muted">
              A planned film of the hub: the Casino, the Penthouse, High Stakes and the Race Track, each with
              its own camera moves, title cards and captions, from whatever the scenes are showing right now.
            </p>
            <Choice label="Format" value={format} onChange={setFormat}
              options={[['reel', '9:16 Reels'], ['landscape', '16:9']]} />
            <Choice label="Length" value={String(length)} onChange={(v) => setLength(Number(v) as TrailerLength)}
              options={[['30', '30 seconds'], ['60', '60 seconds']]} />
            <label className="mt-3 flex items-center gap-2 text-xs text-white/90">
              <input type="checkbox" checked={record && !!mime} disabled={!mime} onChange={(e) => setRecord(e.target.checked)} data-testid="trailer-record" />
              Record it to a video file ({mime ? mime.ext.toUpperCase() : 'not available in this build'})
            </label>
            {note && <p className="mt-3 break-all text-[11px] text-krypt-muted" data-testid="trailer-note">{note}</p>}
            <div className="mt-5 flex justify-end gap-2">
              <button type="button" onClick={onClose} className="krypt-btn-default text-xs">Close</button>
              <button type="button" onClick={start} className="krypt-btn-primary text-xs" data-testid="trailer-start">
                {record && mime ? <Circle className="h-3.5 w-3.5 fill-current text-red-400" /> : <Clapperboard className="h-3.5 w-3.5" />}
                {record && mime ? 'Roll and record' : 'Roll'}
              </button>
            </div>
            <p className="mt-3 text-[10px] text-krypt-dim">Esc stops a take without saving it.</p>
          </div>
        </div>
      )}
    </div>,
    document.body,
  );
}

function Choice<T extends string>({ label, value, onChange, options }: {
  label: string; value: T; onChange: (v: T) => void; options: [T, string][];
}) {
  return (
    <div className="mb-3">
      <div className="mb-1 text-[10px] uppercase tracking-wider text-krypt-dim">{label}</div>
      <div className="flex gap-2">
        {options.map(([v, l]) => (
          <button
            key={v}
            type="button"
            onClick={() => onChange(v)}
            className={`flex-1 rounded-lg border px-3 py-2 text-xs transition ${value === v ? 'border-krypt-purple/70 bg-krypt-purple/15 text-white' : 'border-white/10 bg-white/[0.03] text-krypt-muted hover:text-white'}`}
          >
            {l}
          </button>
        ))}
      </div>
    </div>
  );
}
