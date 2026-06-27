// Contrato de dados Python -> front (Etapa 4, Fase 0).
//
// Os tipos abaixo descrevem o shape dos `api/*.json` emitidos pelo build.py.
// São o acoplamento seguro entre produtor (Python) e consumidor (Astro): se o
// build.py mudar o shape, o type-check do front quebra de propósito.
//
// Regeneração (após mudança de shape no Python): `npm run gen:types` em web/.
// `data`/`craft`/`stages` são gerados por quicktype a partir de amostras reais;
// `history` é mantido à mão (mapa de chaves dinâmicas).

export type { MarketItem, Grade } from './data.ts';
export type { CraftRecipe } from './craft.ts';
export type { Stage } from './stages.ts';
export type { History, HistorySeries, HistoryPoint } from './history.ts';

import type { MarketItem } from './data.ts';
import type { CraftRecipe } from './craft.ts';
import type { Stage } from './stages.ts';

/** api/data.json — ranking/catálogo do mercado. */
export type MarketData = MarketItem[];

/** api/craft.json — receitas de craft. */
export type CraftData = CraftRecipe[];

/** api/stages.json — estágios/fases (Farm). */
export type StagesData = Stage[];
