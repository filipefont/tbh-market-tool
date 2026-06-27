import { describe, expect, it } from 'vitest';
import { loadCraft, loadGems, loadMarket, loadStages } from '../src/lib/data.ts';
import { iconUrl } from '../src/lib/format.ts';
import { attrLabel, typeLabel, verdictMeta } from '../src/lib/labels.ts';

describe('loader de dados (api -> fixtures)', () => {
  it('carrega mercado, craft e stages não-vazios', () => {
    expect(loadMarket().length).toBeGreaterThan(0);
    expect(loadCraft().length).toBeGreaterThan(0);
    expect(loadStages().length).toBeGreaterThan(0);
  });

  it('loadGems só traz itens com efeitos', () => {
    const gems = loadGems();
    expect(gems.length).toBeGreaterThan(0);
    expect(gems.every((g) => (g.effects?.length ?? 0) > 0)).toBe(true);
  });
});

describe('rótulos', () => {
  it('typeLabel traduz tipos conhecidos', () => {
    expect(typeLabel('Helmet')).toBe('Elmo');
    expect(typeLabel('Desconhecido')).toBe('Desconhecido');
  });

  it('verdictMeta cai em unknown p/ valor inválido', () => {
    expect(verdictMeta('craft').variant).toBe('craft');
    expect(verdictMeta('xyz').variant).toBe('unknown');
    expect(verdictMeta(null).variant).toBe('unknown');
  });

  it('attrLabel humaniza camelCase e Percent', () => {
    expect(attrLabel('LightningDamagePercent')).toBe('Lightning Damage %');
  });
});

describe('iconUrl', () => {
  it('monta a URL da wiki a partir do nome', () => {
    expect(iconUrl('HELMET_500017')).toBe('https://www.taskbarherowiki.com/icons/HELMET_500017.png');
    expect(iconUrl(null)).toBeNull();
  });
});
