import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { OfficialPnl } from '@shared/types';
import { OfficialPnlCard } from '../src/components/DataV2Panels';

function figure(label: string): string {
  const el = screen.getByText(label);
  return el.nextElementSibling?.textContent ?? '';
}

describe('OfficialPnlCard', () => {
  beforeEach(() => {
    const pnl: OfficialPnl = {
      ok: true, interval: '1w', economic_pnl: 100, realized_pnl: 80, unrealized_pnl: 20,
      fees_paid: -12.5, maker_rebate: 1, taker_rebate: 0.5, series: [],
    };
    (window as unknown as { krypt: unknown }).krypt = {
      polydata: { userPnl: vi.fn().mockResolvedValue(pnl) },
    };
  });

  it('prints fees paid as a positive amount', async () => {
    render(<OfficialPnlCard />);
    await screen.findByText('+$100.00');
    expect(figure('Fees paid')).toBe('$12.50');
  });
});
