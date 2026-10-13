import { useEffect, useMemo, useState } from 'react';
import { HubStage } from './HubStage';
import { readScoreboard, type HubScoreboard } from './data';
import { PenthouseEngine } from './penthouse/PenthouseEngine';
import { activityBus } from '../state/activity';

function create(canvas: HTMLCanvasElement, w: number, h: number) {
  return new PenthouseEngine(canvas, w, h);
}

const SCOREBOARD_POLL_MS = 60_000;

export default function PenthouseView() {
  const [board, setBoard] = useState<HubScoreboard | null | undefined>(undefined);

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const b = await readScoreboard();
        if (alive) setBoard(b ?? null);
        if (b === undefined) window.clearInterval(i);
      } catch {
        if (alive) setBoard(null);
      }
    };
    void pull();
    const i = window.setInterval(pull, SCOREBOARD_POLL_MS);
    let soon = 0;
    const off = activityBus.subscribe((rec) => {
      if (rec.ev.kind !== 'agentCall' || rec.ev.tool !== 'record_forecast' || rec.ev.outcome !== 'ok') return;
      window.clearTimeout(soon);
      soon = window.setTimeout(() => { void pull(); }, 1500);
    });
    return () => { alive = false; window.clearInterval(i); window.clearTimeout(soon); off(); };
  }, []);

  const extra = useMemo(() => (board === undefined ? {} : { scoreboard: board }), [board]);

  return (
    <HubStage
      create={create}
      hint="your agents · drag to look around · scroll to zoom · click a desk to sit in"
      loading="Turning the monitors on…"
      tourLabel="Cinematic"
      tourTitle="Cuts to whichever agent just called a tool, then round the desks, the wall and the view"
      frameBg="#05060d"
      walkKeys="E lean in at a desk · the elevator is the way out"
      extra={extra}
    />
  );
}
