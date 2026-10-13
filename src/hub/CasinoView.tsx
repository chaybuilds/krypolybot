import { HubStage } from './HubStage';
import { CasinoEngine } from './engine/CasinoEngine';

function create(canvas: HTMLCanvasElement, w: number, h: number) {
  return new CasinoEngine(canvas, w, h);
}

export default function CasinoView() {
  return (
    <HubStage
      create={create}
      hint="drag to pan · scroll to zoom · click an area to follow it · Walk to play the tables"
      loading="Opening the casino…"
      tourLabel="Tour"
      tourTitle="Camera tours the busiest tables"
      frameBg="#07040a"
      wantsAutopilot
      wantsSportsbook
      walkKeys="Space jump · 1–6 hotbar · F or click to use"
    />
  );
}
