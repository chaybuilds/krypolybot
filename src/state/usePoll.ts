import { useCallback, useEffect, useRef, useState } from 'react';
import { userMessage } from '../utils/errors';

export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  deps: unknown[] = [],
  enabled = true,
): { data: T | null; error: string | null; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;
  const gen = useRef(0);
  const inFlight = useRef(-1);

  const run = useCallback(async (mine: number): Promise<void> => {
    if (inFlight.current === mine) return;
    inFlight.current = mine;
    try {
      const v = await fetchRef.current();
      if (mine !== gen.current) return;
      setData(v);
      setError(null);
    } catch (e) {
      if (mine !== gen.current) return;
      setError(userMessage(e));
    } finally {
      if (inFlight.current === mine) inFlight.current = -1;
      if (mine === gen.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const mine = ++gen.current;
    if (!enabled) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setData(null);
    void run(mine);
    if (intervalMs <= 0) return;
    const id = setInterval(() => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      if (mine === gen.current) void run(mine);
    }, intervalMs);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, intervalMs, enabled, ...deps]);

  const reload = useCallback((): void => { void run(gen.current); }, [run]);
  return { data, error, loading, reload };
}
