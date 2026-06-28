import { describe, expect, it } from 'vitest';
import type { MarketLike } from '../src/lib/market.ts';
import { deriveRows, goldPer, netAfterFee, STEAM_FEE } from '../src/lib/market.ts';

const item = (over: Partial<MarketLike> = {}): MarketLike => ({
  name: 'Test',
  base: 'Test',
  key: 1,
  grade: 'ARCANA',
  gradeRank: 5,
  type: 'GEAR',
  classes: [],
  gearType: 'SWORD',
  level: 10,
  gold: 1000,
  usd: 2,
  listings: 7,
  icon: 'X',
  chg24: 1.5,
  real: { brl: { low: 5, lowText: null, med: null, medText: null, vol: null } },
  book: { brl: { buyMax: 4, buyOrders: 12, sellMin: null, sellOrders: 0, buyBook: [], buyNotional: 0, fetchedAt: 0 } },
  gradeLock: false,
  tradable: true,
  ...over,
});

describe('market', () => {
  it('netAfterFee desconta a taxa da Steam', () => {
    expect(netAfterFee(100)).toBeCloseTo(100 * (1 - STEAM_FEE));
    expect(netAfterFee(null)).toBeNull();
  });

  it('goldPer = gold / preço na moeda', () => {
    expect(goldPer(item(), 'brl')).toBe(1000 / 5); // real.brl.low
    expect(goldPer(item(), 'usd')).toBe(1000 / 2); // usd
    expect(goldPer(item({ usd: null }), 'usd')).toBeNull();
  });

  it('deriveRows monta a linha na moeda escolhida', () => {
    const [r] = deriveRows([item()], 'brl');
    expect(r.price).toBe(5);
    expect(r.goldPer).toBe(200);
    expect(r.buyMax).toBe(4);
    expect(r.buyNet).toBeCloseTo(4 * (1 - STEAM_FEE));
    expect(r.buyOrders).toBe(12);
  });
});
