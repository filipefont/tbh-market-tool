import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { CraftData, History, MarketData, StagesData } from './contract/index.ts';
import type { MarketLike } from './market.ts';

// Carregadores de dados em build-time (SSG). Lêem os api/*.json gerados pelo
// pipeline Python (presentes no deploy e no dev local). Quando ausentes — ex.:
// o build do Playwright no web-ci.yml, que não roda o Python — caem nas amostras
// versionadas em tests/fixtures/, mantendo o build/E2E hermético.
//
// Resolve via process.cwd() (= web/ ao rodar `astro build`/`astro dev`), porque
// import.meta.url passa a apontar p/ o chunk em dist/ depois do bundle SSR.
const ROOT = process.cwd();
const API_DIR = resolve(ROOT, '../api');
const FIXTURE_DIR = resolve(ROOT, 'tests/fixtures');

function load<T>(file: string): T {
  const apiPath = resolve(API_DIR, file);
  const path = existsSync(apiPath) ? apiPath : resolve(FIXTURE_DIR, file);
  return JSON.parse(readFileSync(path, 'utf8')) as T;
}

/** api/data.json — catálogo/ranking do mercado. */
export const loadMarket = (): MarketData => load<MarketData>('data.json');

/** api/craft.json — receitas de craft. */
export const loadCraft = (): CraftData => load<CraftData>('craft.json');

/** api/stages.json — estágios (Farm). */
export const loadStages = (): StagesData => load<StagesData>('stages.json');

/** api/history.json — séries de preço por item. */
export const loadHistory = (): History => load<History>('history.json');

/** Itens que têm efeitos (fonte da aba Efeitos). */
export const loadGems = (): MarketData => loadMarket().filter((d) => d.effects && d.effects.length > 0);

/** Projeção enxuta do mercado p/ embutir no Mercado (só os campos da tabela/hero). */
export function loadMarketSlim(): MarketLike[] {
  const hist = loadHistory();
  // últimos N preços por item, p/ o mini-gráfico (sparkline) dos cards/hero
  const spark = (name: string): number[] | undefined => {
    const s = hist[name];
    if (!s || s.length < 2) return undefined;
    return s.slice(-14).map((p) => p[1]);
  };
  return loadMarket().map((d) => ({
    name: d.name,
    base: d.base,
    key: d.key,
    grade: d.grade,
    gradeRank: d.gradeRank,
    type: d.type,
    gearType: d.gearType,
    classes: d.classes,
    attrs: d.attrs,
    level: d.level,
    gold: d.gold,
    usd: d.usd,
    listings: d.listings,
    icon: d.icon,
    chg24: d.chg24,
    chg7: d.chg7,
    real: d.real,
    book: d.book,
    gradeLock: d.gradeLock,
    tradable: d.tradable,
    spark: spark(d.name),
  }));
}
