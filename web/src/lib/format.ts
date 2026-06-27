import type { MarketItem } from './contract/index.ts';

export type Currency = 'brl' | 'usd';

/** Símbolo da moeda. */
export function currencySymbol(cur: Currency): string {
  return cur === 'brl' ? 'R$' : '$';
}

/** Número abreviado: 1234 -> "1.2k", 3_400_000 -> "3.4M" (portado de fmtAbbr). */
export function fmtAbbr(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—';
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, '') + 'B';
  if (abs >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (abs >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(Math.round(n));
}

/** Preço do item na moeda escolhida (BRL = encomenda/real; USD = estimativa). */
export function priceOf(item: MarketItem, cur: Currency): number | null {
  if (cur === 'brl') return item.real?.brl?.low ?? null;
  return item.usd ?? null;
}

/** Preço formatado com símbolo, ex.: "R$ 12.34". */
export function fmtPrice(value: number | null | undefined, cur: Currency): string {
  if (value == null || !Number.isFinite(value)) return '—';
  return `${currencySymbol(cur)} ${value.toFixed(2)}`;
}

/** Variação 24h do item (em %), se houver. */
export function delta24(item: MarketItem): number | null {
  return item.chg24 ?? null;
}

export type Trend = 'up' | 'down' | 'flat';

/** Direção da variação (limiar pequeno conta como estável). */
export function trendOf(pct: number | null | undefined): Trend {
  if (pct == null || Math.abs(pct) < 0.05) return 'flat';
  return pct > 0 ? 'up' : 'down';
}

/** Cor da variação (verde sobe, vermelho desce, neutro estável) — portada do legado. */
export function deltaColor(pct: number | null | undefined): string {
  const t = trendOf(pct);
  return t === 'up' ? '#5fd38d' : t === 'down' ? '#e07a7a' : '#9aa3b8';
}

/** Texto da variação, ex.: "+3.2%" / "-1.0%" / "0%". */
export function fmtDelta(pct: number | null | undefined): string {
  if (pct == null || !Number.isFinite(pct)) return '—';
  const sign = pct > 0 ? '+' : '';
  return `${sign}${pct.toFixed(1)}%`;
}
