import { describe, expect, it, vi } from 'vitest';
import { sanitizeChannelPatch } from '../electron/system/configPatch';

describe('config patches from chat and scripts', () => {
  it("lets /pause's whole patch through, agents included", () => {
    const drop = vi.fn();
    const out = sanitizeChannelPatch('remote', {
      enableTrading: false, crypto15mEnabled: false, copyEnabled: false,
      scriptsLiveEnabled: false, perpsEnabled: false,
      mcpTradeMode: 'off', autopilotEnabled: false,
    }, drop);
    expect(drop).not.toHaveBeenCalled();
    expect(out).toEqual({
      enableTrading: false, crypto15mEnabled: false, copyEnabled: false,
      scriptsLiveEnabled: false, perpsEnabled: false,
      mcpTradeMode: 'off', autopilotEnabled: false,
    });
  });

  it('never lets a channel turn the agents ON or LIVE', () => {
    const drop = vi.fn();
    for (const source of ['remote', 'script']) {
      const out = sanitizeChannelPatch(source, { mcpTradeMode: 'live', autopilotEnabled: true }, drop);
      expect(out).toEqual({});
    }
    expect(sanitizeChannelPatch('remote', { mcpTradeMode: 'paper' }, drop)).toEqual({ mcpTradeMode: 'paper' });
    expect(sanitizeChannelPatch('remote', { mcpTradeMode: 'bogus' }, drop)).toEqual({});
  });

  it('/resume may put engines back; a script may only turn them off', () => {
    const drop = vi.fn();
    expect(sanitizeChannelPatch('remote', { enableTrading: true, perpsEnabled: true }, drop))
      .toEqual({ enableTrading: true, perpsEnabled: true });
    expect(sanitizeChannelPatch('script', { enableTrading: true, perpsEnabled: true }, drop)).toEqual({});
    expect(sanitizeChannelPatch('script', { crypto15mDirectionMode: 'model', crypto15mEntryMax: 80 }, drop))
      .toEqual({ crypto15mDirectionMode: 'model', crypto15mEntryMax: 80 });
  });

  it('drops anything not on the list, and mistyped values', () => {
    const drop = vi.fn();
    expect(sanitizeChannelPatch('remote', {
      accountMode: 'live', mcpLiveApproval: false, enableTrading: 'yes', crypto15mEntryMax: Infinity,
    }, drop)).toEqual({});
    expect(drop).toHaveBeenCalledTimes(4);
  });
});
