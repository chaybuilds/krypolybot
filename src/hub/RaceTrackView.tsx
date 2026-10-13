import { HubStage } from './HubStage';
import { RaceTrackEngine } from './racetrack/RaceTrackEngine';

function create(canvas: HTMLCanvasElement, w: number, h: number) {
  return new RaceTrackEngine(canvas, w, h);
}

export default function RaceTrackView() {
  return (
    <HubStage
      create={create}
      hint="drag to orbit · scroll to zoom · click a racer to follow it"
      loading="Raising the floodlights…"
      tourLabel="Cinematic"
      tourTitle="Camera rides each runner to the gate, then cuts to the finish"
      frameBg="#04060d"
    />
  );
}
