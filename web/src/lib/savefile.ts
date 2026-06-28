import type { MarketLike } from './market.ts';
import { netAfterFee } from './market.ts';

// Leitor do save do jogo (SaveFile_Live.es3) — SOMENTE LEITURA. Decripta e parseia
// 100% no navegador (Web Crypto); nunca escreve/re-salva o arquivo. Formato: Easy
// Save 3 / AES-128-CBC com chave PBKDF2-SHA1(senha, salt=IV, 100, 16); IV = 16
// primeiros bytes. A senha é a do próprio jogo (constante do ES3); só lemos o save
// do próprio usuário, localmente.
const ES3_PASSWORD = 'emuMqG3bLYJ938ZDCfieWJ';
const GOLD_KEY = 100001;

/**
 * Normaliza o ItemKey do save para a chave do catálogo: variantes terminadas em
 * 900 (ex.: 190004900 -> 190004) mapeiam para o item-base. Verificado: 100% de
 * cobertura no save de exemplo.
 */
export function normalizeKey(key: number, known: (k: number) => boolean): number {
  if (known(key)) return key;
  if (key > 1_000_000 && key % 1000 === 900) return Math.floor(key / 1000);
  return key;
}

/** Decripta o .es3 (ArrayBuffer) e devolve o JSON em texto. */
export async function decryptSave(buf: ArrayBuffer): Promise<string> {
  const bytes = new Uint8Array(buf);
  const iv = bytes.slice(0, 16);
  const ct = bytes.slice(16);
  const baseKey = await crypto.subtle.importKey('raw', new TextEncoder().encode(ES3_PASSWORD), 'PBKDF2', false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits({ name: 'PBKDF2', salt: iv, iterations: 100, hash: 'SHA-1' }, baseKey, 128);
  const aesKey = await crypto.subtle.importKey('raw', bits, { name: 'AES-CBC' }, false, ['decrypt']);
  const plain = await crypto.subtle.decrypt({ name: 'AES-CBC', iv }, aesKey, ct);
  return new TextDecoder().decode(plain);
}

export interface ParsedSave {
  gold: number;
  /** quantidade por ItemKey (já agregada). */
  counts: Map<number, number>;
  totalItems: number;
}

/** Parseia o JSON decriptado em ouro + contagem por ItemKey. */
export function parseSave(plain: string): ParsedSave {
  const es3 = JSON.parse(plain);
  const psd = JSON.parse(es3?.PlayerSaveData?.value ?? '{}');
  const counts = new Map<number, number>();
  let totalItems = 0;
  for (const it of psd.itemSaveDatas ?? []) {
    const k = it.ItemKey as number;
    if (k == null) continue;
    counts.set(k, (counts.get(k) ?? 0) + 1);
    totalItems++;
  }
  const gold = (psd.currenySaveDatas ?? []).find((c: { Key: number }) => c.Key === GOLD_KEY)?.Quantity ?? 0;
  return { gold, counts, totalItems };
}

export interface BagLine {
  key: number;
  name: string;
  grade: string;
  icon: string | null;
  count: number;
  unit: number | null; // valor unitário (BRL, líquido da encomenda ou preço)
  total: number; // unit * count (0 se sem preço)
}

export interface BagValue {
  gold: number;
  totalBRL: number;
  totalItems: number;
  priced: number; // unidades com preço
  unpriced: number; // unidades sem preço de mercado
  unmatched: number; // unidades sem item no catálogo
  lines: BagLine[]; // ordenadas por total desc
}

/** Valor unitário em BRL: líquido da maior encomenda; senão o menor preço real. */
function unitValueBRL(item: MarketLike): number | null {
  const net = netAfterFee(item.book?.brl?.buyMax ?? null);
  if (net != null) return net;
  return item.real?.brl?.low ?? null;
}

/** Cruza o save com o catálogo e valora a mochila pela encomenda líquida (BRL). */
export function valueBag(parsed: ParsedSave, items: MarketLike[]): BagValue {
  const byKey = new Map<number, MarketLike>();
  for (const it of items) if (it.key != null) byKey.set(it.key, it);
  const known = (k: number) => byKey.has(k);

  const lines: BagLine[] = [];
  let totalBRL = 0;
  let priced = 0;
  let unpriced = 0;
  let unmatched = 0;

  for (const [rawKey, count] of parsed.counts) {
    const item = byKey.get(normalizeKey(rawKey, known));
    if (!item) {
      unmatched += count;
      continue;
    }
    const unit = unitValueBRL(item);
    const total = unit != null ? unit * count : 0;
    if (unit != null) priced += count;
    else unpriced += count;
    totalBRL += total;
    lines.push({ key: rawKey, name: item.name, grade: item.grade, icon: item.icon, count, unit, total });
  }

  lines.sort((a, b) => b.total - a.total);
  return { gold: parsed.gold, totalBRL, totalItems: parsed.totalItems, priced, unpriced, unmatched, lines };
}
