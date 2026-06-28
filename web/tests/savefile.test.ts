import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import type { MarketLike } from '../src/lib/market.ts';
import { decryptSave, normalizeKey, parseSave, valueBag } from '../src/lib/savefile.ts';

const fixturePath = (name: string) => fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url));
const items = JSON.parse(readFileSync(fixturePath('data.json'), 'utf8')) as MarketLike[];
const saveBuf = readFileSync(fixturePath('sample.es3'));

describe('savefile', () => {
  it('normalizeKey resolve variantes …900 p/ a base', () => {
    const known = (k: number) => k === 505171;
    expect(normalizeKey(505171900, known)).toBe(505171);
    expect(normalizeKey(505171, known)).toBe(505171); // já conhecida
    expect(normalizeKey(115002, known)).toBe(115002); // não-900 fica como está
  });

  it('decripta + parseia o save (Web Crypto)', async () => {
    const plain = await decryptSave(saveBuf.buffer.slice(saveBuf.byteOffset, saveBuf.byteOffset + saveBuf.byteLength));
    const parsed = parseSave(plain);
    expect(parsed.gold).toBe(13371337);
    expect(parsed.totalItems).toBe(4);
    expect(parsed.counts.get(115002)).toBe(2);
  });

  it('valora a mochila pela encomenda líquida (BRL)', async () => {
    const plain = await decryptSave(saveBuf.buffer.slice(saveBuf.byteOffset, saveBuf.byteOffset + saveBuf.byteLength));
    const bag = valueBag(parseSave(plain), items);
    expect(bag.gold).toBe(13371337);
    expect(bag.unmatched).toBe(0); // 505171900 normaliza p/ 505171 (catálogo)
    expect(bag.priced).toBe(4);
    expect(bag.totalBRL).toBeGreaterThan(0);
    expect(bag.lines.length).toBeGreaterThan(0);
  });
});
