import { useEffect, useState } from 'react';

let selected: string | null = null;
const subs = new Set<(v: string | null) => void>();

export function selectMarket(conditionId: string | null): void {
  if (selected === conditionId) return;
  selected = conditionId;
  for (const fn of subs) fn(selected);
}

export function getSelectedMarket(): string | null {
  return selected;
}

export function useSelectedMarket(): string | null {
  const [v, setV] = useState<string | null>(selected);
  useEffect(() => {
    subs.add(setV);
    setV(selected);
    return () => { subs.delete(setV); };
  }, []);
  return v;
}
