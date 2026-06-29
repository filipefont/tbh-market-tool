import type { MarketItem } from './contract/index.ts';
import { type Currency, priceOf } from './format.ts';

// Projeção enxuta usada pela tabela do Mercado — só os campos que a tabela e o
// hero precisam. A página embute isso via SSR (mais leve que o item completo).
export type MarketLike = Pick<
  MarketItem,
  | 'name'
  | 'base'
  | 'key'
  | 'grade'
  | 'gradeRank'
  | 'type'
  | 'gearType'
  | 'classes'
  | 'attrs'
  | 'level'
  | 'gold'
  | 'usd'
  | 'listings'
  | 'icon'
  | 'chg24'
  | 'chg7'
  | 'real'
  | 'book'
  | 'gradeLock'
  | 'tradable'
> & {
  /** série compacta de preços (últimos pontos) p/ o mini-gráfico. */
  spark?: number[];
};

// Taxa da Steam aplicada ao vender numa encomenda (buy order) — o vendedor recebe
// o líquido. Espelha o "−15%" do legado.
export const STEAM_FEE = 0.15;

/** Líquido recebido ao vender numa encomenda (desconta a taxa da Steam). */
export function netAfterFee(buyMax: number | null | undefined): number | null {
  return buyMax == null ? null : buyMax * (1 - STEAM_FEE);
}

/** Maior encomenda ativa (buy order) em BRL — dado real do order book. */
export function buyMax(item: MarketLike): number | null {
  return item.book?.brl?.buyMax ?? null;
}

/** Total de encomendas (demanda agregada). */
export function buyOrders(item: MarketLike): number {
  return item.book?.brl?.buyOrders ?? 0;
}

/** Gold por unidade de moeda (gold ÷ preço) — quanto gold cada 1 R$/$ compra. */
export function goldPer(item: MarketLike, cur: Currency): number | null {
  const p = priceOf(item, cur);
  return p && p > 0 ? item.gold / p : null;
}

/** Linha derivada usada pela tabela do Mercado (campos prontos p/ ordenar). */
export interface MarketRow {
  item: MarketLike;
  name: string;
  grade: string;
  gradeRank: number;
  type: string;
  gearType: string;
  classes: string[];
  level: number | null;
  gold: number;
  price: number | null;
  chg24: number | null;
  chg7: number | null;
  goldPer: number | null;
  listings: number;
  vol: number | null;
  buyMax: number | null;
  buyNet: number | null;
  buyOrders: number;
  tradable: boolean;
}

/** Deriva as linhas da tabela na moeda escolhida. */
export function deriveRows(items: MarketLike[], cur: Currency): MarketRow[] {
  return items.map((item) => {
    const bm = buyMax(item);
    return {
      item,
      name: item.name,
      grade: item.grade,
      gradeRank: item.gradeRank,
      type: item.type ?? '',
      gearType: item.gearType ?? '',
      classes: item.classes ?? [],
      level: item.level,
      gold: item.gold,
      price: priceOf(item, cur),
      chg24: item.chg24 ?? null,
      chg7: item.chg7 ?? null,
      goldPer: goldPer(item, cur),
      listings: item.listings,
      vol: item.real?.brl?.vol ?? null,
      buyMax: bm,
      buyNet: netAfterFee(bm),
      buyOrders: buyOrders(item),
      tradable: item.tradable,
    };
  });
}
