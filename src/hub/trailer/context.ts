import { createContext } from 'react';
import type { CineShot } from '../cine';

export interface TrailerTarget {
  canvas: HTMLCanvasElement;
  w: number;
  h: number;
  shot: CineShot | null;
  shotKey: number;
}

export const TrailerContext = createContext<TrailerTarget | null>(null);
