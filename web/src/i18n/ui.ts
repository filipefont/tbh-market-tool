// Catálogo de mensagens da "casca" (Fase 4b). PT é a fonte; EN traduz. Dados do
// jogo (nomes de itens) não são traduzidos por ora. Astro.currentLocale dá o lang.
export const languages = { pt: 'Português', en: 'English' } as const;
export type Lang = keyof typeof languages;
export const defaultLang: Lang = 'pt';

export function isLang(v: string | undefined): v is Lang {
  return v === 'pt' || v === 'en';
}

export const ui = {
  pt: {
    'nav.ranking': '◆ Ranking',
    'nav.effects': '✦ Efeitos',
    'nav.farm': '⛏ Farm',
    'nav.craft': '⚒ Craft',
    'nav.lang': 'Idioma',
    'mercado.title': 'Mercado',
    'mercado.subtitle': 'Ordene por qualquer coluna; Gold/moeda mostra o melhor negócio. Encomendas e líquido (−15% taxa) em R$.',
    'mercado.search': 'buscar item…',
    'mercado.allGrades': 'todas as grades',
    'mercado.items': 'itens',
    'col.item': 'Item',
    'col.grade': 'Grade',
    'col.type': 'Tipo',
    'col.lvl': 'Lvl',
    'col.gold': 'Gold',
    'col.price': 'Preço',
    'col.delta': 'Δ24h',
    'col.goldPer': 'Gold/moeda',
    'col.listings': 'List.',
    'col.buyMax': 'Maior enc.',
    'col.net': 'Líquido',
    'col.orders': 'Encom.',
    'effects.title': 'Efeitos',
    'effects.subtitle': 'gemas e seus efeitos por slot, com o preço de mercado.',
    'farm.title': 'Farm',
    'farm.subtitle': 'estágios, ordenados pelo valor tradável esperado por caixa (EV, em USD).',
    'craft.title': 'Craft',
    'craft.subtitle': 'receitas. Compara o custo dos reagentes com o valor esperado de revenda. Preços em USD.',
  },
  en: {
    'nav.ranking': '◆ Ranking',
    'nav.effects': '✦ Effects',
    'nav.farm': '⛏ Farm',
    'nav.craft': '⚒ Craft',
    'nav.lang': 'Language',
    'mercado.title': 'Market',
    'mercado.subtitle': 'Sort by any column; Gold/currency shows the best deal. Buy orders and net (−15% fee) in R$.',
    'mercado.search': 'search item…',
    'mercado.allGrades': 'all grades',
    'mercado.items': 'items',
    'col.item': 'Item',
    'col.grade': 'Grade',
    'col.type': 'Type',
    'col.lvl': 'Lvl',
    'col.gold': 'Gold',
    'col.price': 'Price',
    'col.delta': 'Δ24h',
    'col.goldPer': 'Gold/cur.',
    'col.listings': 'List.',
    'col.buyMax': 'Top order',
    'col.net': 'Net',
    'col.orders': 'Orders',
    'effects.title': 'Effects',
    'effects.subtitle': 'gems and their effects per slot, with market price.',
    'farm.title': 'Farm',
    'farm.subtitle': 'stages, sorted by expected tradable value per box (EV, in USD).',
    'craft.title': 'Craft',
    'craft.subtitle': 'recipes. Compares reagent cost with expected resale value. Prices in USD.',
  },
} as const;

export type UIKey = keyof (typeof ui)['pt'];

/** Tradutor p/ um idioma (cai no PT se faltar a chave). */
export function useTranslations(lang: Lang) {
  return (key: UIKey): string => ui[lang][key] ?? ui[defaultLang][key];
}

/** Resolve o idioma atual a partir do Astro.currentLocale. */
export function resolveLang(currentLocale: string | undefined): Lang {
  return isLang(currentLocale) ? currentLocale : defaultLang;
}
