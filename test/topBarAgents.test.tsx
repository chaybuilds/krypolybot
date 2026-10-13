import type { ReactNode } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const app = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warn: vi.fn(), info: vi.fn(), push: vi.fn() }));
vi.mock('../src/state/AppStateProvider', () => ({ useApp: () => app.value }));
vi.mock('../src/state/ToastProvider', () => ({ useToast: () => toast }));
vi.mock('../src/components/BossFight', () => ({ BossWidget: () => null }));
vi.mock('../src/components/glass/GlassPanel', () => ({
  GlassPanel: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));
vi.mock('../src/components/glass/GlassSegmented', () => ({ GlassSegmented: () => null }));

import { TopBar } from '../src/components/TopBar';
import { GoLiveAccountDialog } from '../src/components/AccountModePanel';

const update = vi.fn(async () => ({}));

const LIVE_AGENT = { id: 'default', name: 'Default', enabled: true, mode: 'live' };

function setConfig(config: Record<string, unknown>): void {
  app.value = {
    config,
    account: null,
    backend: { status: 'running' },
    state: { uiMode: 'advanced' },
    positions: [],
    credentialsAll: null,
    refresh: {
      state: vi.fn(async () => undefined), credentials: vi.fn(async () => undefined),
      account: vi.fn(async () => undefined), backend: vi.fn(async () => undefined),
      positions: vi.fn(async () => undefined),
    },
  };
}

beforeEach(() => {
  update.mockClear();
  (window as unknown as { krypt: unknown }).krypt = {
    config: { update },
    state: { setUiMode: vi.fn() },
    terminal: { standing: vi.fn(async () => ({ instructions: [] })) },
    credentials: { test: vi.fn() },
  };
});

describe('the header and the AI agents', () => {
  it('does not say "All engines paused" while agents trade real money', () => {
    setConfig({ accountMode: 'live', mcpTradeMode: 'live', mcpEnabled: true,
      mcpLiveApproval: false, mcpAgents: [LIVE_AGENT] });
    render(<TopBar />);
    expect(screen.getByRole('heading').textContent).toBe('AI agents live');
    expect(screen.getByTestId('topbar-stop-agents')).toBeTruthy();
    expect(screen.getByTestId('topbar-agents-chip').getAttribute('title')).toContain('approvals OFF');
  });

  it('counts agents alongside live engines', () => {
    setConfig({ accountMode: 'live', enableTrading: true, mcpTradeMode: 'live',
      mcpEnabled: true, mcpAgents: [LIVE_AGENT] });
    render(<TopBar />);
    expect(screen.getByRole('heading').textContent).toBe('1 engine + AI agents live');
  });

  it('Stop agents asks first, then puts the agents back on paper', async () => {
    setConfig({ accountMode: 'live', mcpTradeMode: 'live', autopilotEnabled: true,
      mcpAgents: [LIVE_AGENT] });
    render(<TopBar />);
    fireEvent.click(screen.getByTestId('topbar-stop-agents'));
    expect(update).not.toHaveBeenCalled();
    expect(screen.getByText('Stop your AI agents trading real money?')).toBeTruthy();
    await act(async () => { const btns = screen.getAllByRole('button', { name: 'Stop agents' }); fireEvent.click(btns[btns.length - 1]); });
    expect(update).toHaveBeenCalledWith({ mcpTradeMode: 'paper' });
    expect(toast.success).toHaveBeenCalled();
  });

  it('agents on paper or off: no stop button, the engines headline as before', () => {
    setConfig({ accountMode: 'live', mcpTradeMode: 'paper', mcpEnabled: true, mcpAgents: [LIVE_AGENT] });
    const { unmount } = render(<TopBar />);
    expect(screen.getByRole('heading').textContent).toBe('All engines paused');
    expect(screen.queryByTestId('topbar-stop-agents')).toBeNull();
    expect(screen.getByTestId('topbar-agents-chip').getAttribute('title')).toContain('paper');
    unmount();
    setConfig({ accountMode: 'paper', mcpTradeMode: 'live', mcpEnabled: true, mcpAgents: [LIVE_AGENT] });
    render(<TopBar />);
    expect(screen.queryByTestId('topbar-stop-agents')).toBeNull();
  });
});

describe("the account's Go live", () => {
  it('says the agents stay on paper and links to their page', () => {
    setConfig({ accountMode: 'paper', mcpTradeMode: 'paper', mcpEnabled: true, mcpAgents: [LIVE_AGENT] });
    const onNav = vi.fn();
    const onClose = vi.fn();
    render(<GoLiveAccountDialog onClose={onClose} onNav={onNav} />);
    expect(screen.getByTestId('golive-account-agents-paper').textContent)
      .toContain('Your AI agents stay on paper — go live for them on the AI Agents page');
    fireEvent.click(screen.getByTestId('golive-account-agents-link'));
    expect(onNav).toHaveBeenCalledWith('agents');
    expect(onClose).toHaveBeenCalled();
  });

  it('says nothing of the kind when an agent is already set live', () => {
    setConfig({ accountMode: 'paper', mcpTradeMode: 'live', mcpEnabled: true, mcpAgents: [LIVE_AGENT] });
    render(<GoLiveAccountDialog onClose={() => undefined} />);
    expect(screen.queryByTestId('golive-account-agents-paper')).toBeNull();
  });
});
