import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import type { CraftData, History, MarketData, StagesData } from '../src/lib/contract/index.ts';

// Guarda do contrato Python -> front: lê os api/*.json REAIS emitidos pelo build.py
// e confere que ainda casam com os tipos do contrato. Se o shape mudar no Python
// sem atualizar os tipos, este teste (ou o type-check) acusa.
const api = (name: string) =>
  JSON.parse(readFileSync(fileURLToPath(new URL(`../../api/${name}`, import.meta.url)), 'utf8'));

describe('contrato api/*.json', () => {
  it('data.json é uma lista de itens de mercado', () => {
    const data = api('data.json') as MarketData;
    expect(Array.isArray(data)).toBe(true);
    expect(data.length).toBeGreaterThan(0);
    const it0 = data[0];
    expect(typeof it0.name).toBe('string');
    expect(typeof it0.gold).toBe('number');
  });

  it('history.json é um mapa nome -> série de [ts, preço]', () => {
    const hist = api('history.json') as History;
    const series = Object.values(hist)[0];
    expect(Array.isArray(series)).toBe(true);
    expect(series[0]).toHaveLength(2);
    expect(typeof series[0][0]).toBe('number');
  });

  it('craft.json é uma lista de receitas', () => {
    const craft = api('craft.json') as CraftData;
    expect(Array.isArray(craft)).toBe(true);
    expect(typeof craft[0].type).toBe('string');
  });

  it('stages.json é uma lista de estágios', () => {
    const stages = api('stages.json') as StagesData;
    expect(Array.isArray(stages)).toBe(true);
    expect(typeof stages[0].level).toBe('number');
  });
});
