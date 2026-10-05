export interface PollTimers {
  setInterval: (fn: () => void, ms: number) => number;
  clearInterval: (id: number) => void;
  isHidden: () => boolean;
}

export interface PollController {
  start: () => void;
  onVisibilityChange: () => void;
  runNow: () => void;
  stop: () => void;
  isArmed: () => boolean;
}

export function createPoll(
  task: () => Promise<unknown>,
  intervalMs: number,
  timers: PollTimers,
): PollController {
  let id: number | null = null;
  let busy = false;
  let stopped = false;

  const runNow = (): void => {
    if (stopped || busy) return;
    busy = true;
    void Promise.resolve()
      .then(task)
      .catch(() => {})
      .finally(() => { busy = false; });
  };

  const arm = (): void => {
    if (stopped || id !== null || timers.isHidden()) return;
    id = timers.setInterval(runNow, intervalMs);
  };

  const disarm = (): void => {
    if (id === null) return;
    timers.clearInterval(id);
    id = null;
  };

  return {
    start: () => {
      if (stopped) return;
      arm();
    },
    onVisibilityChange: () => {
      if (stopped) return;
      if (timers.isHidden()) {
        disarm();
        return;
      }
      if (id !== null) return;
      arm();
      runNow();
    },
    runNow,
    stop: () => {
      stopped = true;
      disarm();
    },
    isArmed: () => id !== null,
  };
}
