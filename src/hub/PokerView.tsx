import { useEffect, useMemo, useState } from 'react';
import { HubStage } from './HubStage';
import { readScoreboard, type HubScoreboard } from './data';
import { PokerEngine } from './poker/PokerEngine';
import { MIN_REAL_FORECASTERS, assignSeats, forecastersFrom, tableMode, type TableChoice } from './poker/seats';
import { GlassButton } from '../components/glass/GlassButton';
import { useApp } from '../state/AppStateProvider';
import { configuredAgents } from '../utils/agents';

function create(canvas: HTMLCanvasElement, w: number, h: number) {
  return new PokerEngine(canvas, w, h);
}

const SCOREBOARD_POLL_MS = 60_000;
const CHOICE_KEY = 'krypt.council.seats';

function loadChoice(): TableChoice | null {
  try {
    const v = window.localStorage.getItem(CHOICE_KEY);
    return v === 'personas' || v === 'agents' ? v : null;
  } catch {
    return null;
  }
}

export default function PokerView() {
  const [board, setBoard] = useState<HubScoreboard | null | undefined>(undefined);
  const [choice, setChoice] = useState<TableChoice | null>(loadChoice);
  const { config } = useApp();

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
    return () => { alive = false; window.clearInterval(i); };
  }, []);

  const agents = useMemo(() => configuredAgents(config), [config]);
  const real = useMemo(() => assignSeats(forecastersFrom(board, agents)).length, [board, agents]);
  const mode = tableMode(choice, real);
  const pick = (c: TableChoice) => {
    setChoice(c);
    try { window.localStorage.setItem(CHOICE_KEY, c); } catch { }
  };
  const extra = useMemo(() => ({ scoreboard: board, councilChoice: choice }), [board, choice]);
  const agentsOff = real < MIN_REAL_FORECASTERS;

  const controls = (
    <div className="flex items-center gap-1" role="group" aria-label="Who plays at the table">
      <GlassButton
        live={false}
        variant={mode === 'personas' ? 'active' : 'default'}
        onClick={() => pick('personas')}
        title="Six trading emotions play the bot's signals"
        className="px-2.5 py-1.5 text-xs"
      >
        Personas
      </GlassButton>
      <GlassButton
        live={false}
        variant={mode === 'agents' ? 'active' : 'default'}
        onClick={() => pick('agents')}
        disabled={agentsOff}
        title={agentsOff
          ? `Needs ${MIN_REAL_FORECASTERS} real forecasters: named agents calling record_forecast (${real} so far)`
          : 'Your real forecasters: their fair values, bet against the market'}
        className="px-2.5 py-1.5 text-xs"
      >
        Your agents
      </GlassButton>
    </div>
  );

  return (
    <HubStage
      create={create}
      hint={mode === 'agents'
        ? 'your forecasters · drag to look around · click a seat to follow it'
        : 'drag to look around · scroll to zoom · click a player to follow them'}
      loading="Shuffling up and dealing…"
      tourLabel="Cinematic"
      tourTitle="Camera cuts to whoever is talking, then to the showdown"
      frameBg="#080605"
      extra={extra}
      controls={controls}
    />
  );
}
