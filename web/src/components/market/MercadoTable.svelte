<script lang="ts">
  import { type Currency, deltaColor, fmtAbbr, fmtPrice, iconUrl, steamUrl } from '../../lib/format.ts';
  import { gradeColor } from '../../lib/grades.ts';
  import { attrLabel } from '../../lib/labels.ts';
  import { BOOK_CUR, type MarketLike, type MarketRow, deriveRows } from '../../lib/market.ts';
  import { slugify } from '../../lib/slug.ts';
  import { curA, sortDirA, sortKeyA, viewA, toggleFav, wireUrlSync } from '../../lib/stores/market.ts';
  import { ms } from '../../lib/stores/marketState.svelte.ts';
  import CurrencyToggle from '../ds/CurrencyToggle.svelte';
  import Delta from '../ds/Delta.svelte';
  import GradeBadge from '../ds/GradeBadge.svelte';
  import ItemCard from '../ds/ItemCard.svelte';
  import Sparkline from '../ds/Sparkline.svelte';

  // Tabela/cards do Mercado. Os FILTROS vivem na sidebar (MarketFilters); aqui só
  // controles de apresentação (visão, ordenação, moeda) + hero + tabela/cards.
  // Estado compartilhado via store (ms / atoms).
  interface Msgs {
    items: string;
    viewTable: string;
    viewCards: string;
    sortBy: string;
    movers: string;
    heroDeal: string;
    heroCube: string;
    cols: Record<SortKey, string>;
  }
  interface Props {
    items: MarketLike[];
    msgs: Msgs;
  }
  let { items, msgs }: Props = $props();

  $effect(() => wireUrlSync());

  type SortKey =
    | 'name' | 'grade' | 'gearType' | 'classes' | 'level' | 'gold' | 'price'
    | 'chg24' | 'goldPer' | 'listings' | 'vol' | 'buyMax' | 'buyNet' | 'buyOrders';

  const COLUMNS: { key: SortKey; align: 'left' | 'right' }[] = [
    { key: 'name', align: 'left' },
    { key: 'grade', align: 'left' },
    { key: 'gearType', align: 'left' },
    { key: 'classes', align: 'left' },
    { key: 'level', align: 'right' },
    { key: 'gold', align: 'right' },
    { key: 'price', align: 'right' },
    { key: 'chg24', align: 'right' },
    { key: 'goldPer', align: 'right' },
    { key: 'listings', align: 'right' },
    { key: 'vol', align: 'right' },
    { key: 'buyMax', align: 'right' },
    { key: 'buyNet', align: 'right' },
    { key: 'buyOrders', align: 'right' },
  ];
  // colunas oferecidas no seletor de ordenação (visão de cards/qualquer)
  const SORT_KEYS: SortKey[] = ['goldPer', 'price', 'gold', 'chg24', 'listings', 'buyMax', 'name', 'grade', 'level'];
  // larguras generosas, fiéis à densidade da tabela do Cubo (linhas altas, fontes
  // 13px, Gold/moeda largo p/ a barra de proporção).
  const GRID =
    'minmax(220px,2.2fr) 104px 100px 100px 58px 92px 100px 80px 128px 66px 76px 104px 104px 86px';
  const ROW_H = 46;
  const OVERSCAN = 8;
  const CARDS_CAP = 120;

  const base = import.meta.env.BASE_URL.replace(/\/$/, '');
  const itemHref = (name: string) => `${base}/item/${slugify(name)}`;
  const num = (v: string) => (v === '' ? null : Number(v));

  let scrollTop = $state(0);
  let viewportH = $state(600);

  const cur = $derived(ms.cur as Currency);
  const isFav = (name: string) => ms.favs.includes(name);

  // ---- filtra -> ordena (lê o store ms) ----------------------------------------------
  const rows = $derived(deriveRows(items, cur));
  const filtered = $derived.by(() => {
    const ql = ms.q.toLowerCase();
    const lmin = num(ms.lvlMin), lmax = num(ms.lvlMax);
    const gmin = num(ms.goldMin), gmax = num(ms.goldMax);
    const pmin = num(ms.priceMin), pmax = num(ms.priceMax);
    return rows.filter((r) => {
      if (ql && !r.name.toLowerCase().includes(ql)) return false;
      if (ms.grade.length && !ms.grade.includes(r.grade)) return false;
      if (ms.type.length && !ms.type.includes(r.type)) return false;
      if (ms.gtype.length && !ms.gtype.includes(r.gearType)) return false;
      if (ms.cls.length && !ms.cls.some((c) => r.classes.includes(c))) return false;
      if (ms.attr.length && !ms.attr.every((a) => r.item.attrs && a in r.item.attrs)) return false;
      if (lmin != null && !(r.level != null && r.level >= lmin)) return false;
      if (lmax != null && !(r.level != null && r.level <= lmax)) return false;
      if (gmin != null && r.gold < gmin) return false;
      if (gmax != null && r.gold > gmax) return false;
      if (pmin != null && !(r.price != null && r.price >= pmin)) return false;
      if (pmax != null && !(r.price != null && r.price <= pmax)) return false;
      if (ms.onlyTrad && !r.tradable) return false;
      if (ms.onlyBook && r.buyMax == null) return false;
      if (ms.onlyFav && !isFav(r.name)) return false;
      return true;
    });
  });
  function sortValue(r: MarketRow, key: string): number | string | null {
    if (key === 'grade') return r.gradeRank;
    if (key === 'classes') return r.classes.join(',');
    if (key.startsWith('attr:')) return r.item.attrs?.[key.slice(5)]?.value ?? null;
    return r[key as SortKey];
  }
  const sorted = $derived.by(() => {
    const dir = ms.sortDir === 'asc' ? 1 : -1;
    return [...filtered].sort((a, b) => {
      const x = sortValue(a, ms.sortKey);
      const y = sortValue(b, ms.sortKey);
      if (typeof x === 'string' || typeof y === 'string') return String(x).localeCompare(String(y)) * dir;
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return (x - y) * dir;
    });
  });
  const best = $derived(sorted[0]);
  const movers = $derived.by(() => {
    const withc = rows.filter((r) => r.chg24 != null);
    const up = [...withc].filter((r) => (r.chg24 ?? 0) > 0).sort((a, b) => (b.chg24 ?? 0) - (a.chg24 ?? 0)).slice(0, 5);
    const down = [...withc].filter((r) => (r.chg24 ?? 0) < 0).sort((a, b) => (a.chg24 ?? 0) - (b.chg24 ?? 0)).slice(0, 5);
    return [...up, ...down];
  });
  const heroSub = $derived(
    best
      ? [best.gearType ? best.gearType.charAt(0) + best.gearType.slice(1).toLowerCase() : null, best.level != null ? `lvl ${best.level}` : null].filter(Boolean).join(' · ')
      : '',
  );

  const total = $derived(sorted.length);
  const start = $derived(Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN));
  const end = $derived(Math.min(total, start + Math.ceil(viewportH / ROW_H) + OVERSCAN * 2));
  const visible = $derived(sorted.slice(start, end));
  const cardsList = $derived(sorted.slice(0, CARDS_CAP));
  const attrCols = $derived(ms.attr);
  const gridTemplate = $derived(GRID + attrCols.map(() => ' 104px').join(''));
  // maior gold/moeda da seleção — base da barra de proporção (coluna Gold/moeda)
  const maxGoldPer = $derived(Math.max(1, ...filtered.map((r) => r.goldPer ?? 0)));

  function toggleSort(key: string) {
    if (ms.sortKey === key) sortDirA.set(ms.sortDir === 'asc' ? 'desc' : 'asc');
    else {
      sortKeyA.set(key);
      sortDirA.set(key === 'name' || key === 'gearType' || key === 'classes' ? 'asc' : 'desc');
    }
  }
  const money = (v: number | null) => fmtPrice(v, cur);
</script>

<div class="space-y-3">
  <!-- controles de apresentação (barra à direita, fiel ao #marketControls do Cubo) -->
  <div class="flex flex-wrap items-center justify-end gap-2">
    <div class="inline-flex overflow-hidden rounded-md border border-line" role="group">
      <button type="button" aria-pressed={ms.view === 'cards'} onclick={() => viewA.set('cards')} class="px-2.5 py-1.5 text-xs {ms.view === 'cards' ? 'bg-accent text-accent-ink font-semibold' : 'bg-field text-muted hover:bg-row-hover'}">{msgs.viewCards}</button>
      <button type="button" aria-pressed={ms.view === 'table'} onclick={() => viewA.set('table')} class="px-2.5 py-1.5 text-xs {ms.view === 'table' ? 'bg-accent text-accent-ink font-semibold' : 'bg-field text-muted hover:bg-row-hover'}">{msgs.viewTable}</button>
    </div>

    <label class="flex items-center gap-1.5 text-xs text-muted">
      {msgs.sortBy}
      <select value={ms.sortKey} onchange={(e) => sortKeyA.set(e.currentTarget.value)} class="rounded-md border border-line bg-field px-2 py-1.5 text-xs text-ink focus-visible:outline-2 focus-visible:outline-accent-bright">
        {#each SORT_KEYS as k (k)}<option value={k}>{msgs.cols[k]}</option>{/each}
      </select>
    </label>
    <button type="button" onclick={() => sortDirA.set(ms.sortDir === 'asc' ? 'desc' : 'asc')} title="direção" class="rounded-md border border-line bg-field px-2 py-1.5 text-xs text-muted hover:text-ink">{ms.sortDir === 'asc' ? '▲' : '▼'}</button>

    <CurrencyToggle value={cur} onchange={(c) => curA.set(c)} />
    <span class="tabular text-xs text-muted">· {total} {msgs.items}</span>
  </div>

  <!-- top movers -->
  {#if movers.length}
    <div class="flex flex-wrap items-center gap-1.5">
      <span class="text-xs text-muted">{msgs.movers}</span>
      {#each movers as m (m.name)}
        <a href={itemHref(m.name)} class="tabular inline-flex items-center gap-1 rounded-full border border-line bg-field px-2 py-0.5 text-[11px] hover:border-accent-bright" style:color={(m.chg24 ?? 0) > 0 ? '#5fd38d' : '#e07a7a'} title={m.name}>
          {(m.chg24 ?? 0) > 0 ? '▲' : '▼'} <span class="max-w-28 truncate">{m.item.base || m.name}</span> <b>{(m.chg24 ?? 0) > 0 ? '+' : ''}{m.chg24}%</b>
        </a>
      {/each}
    </div>
  {/if}

  <!-- hero -->
  {#if best}
    <div class="grid grid-cols-[1fr_auto] items-center gap-[18px] rounded-2xl border border-[#1e2b27] p-[20px_22px]" style:background="linear-gradient(120deg,#11201b,#0e1318 72%)">
      <div class="min-w-0">
        <div class="flex flex-wrap items-center gap-2">
          <span class="rounded-md bg-gold px-2 py-[3px] text-[11px] font-bold text-[#1c1404]">★ TOP</span>
          <span class="rounded-full bg-accent px-2.5 py-[3px] text-[11px] font-bold text-accent-ink">{msgs.heroDeal}</span>
        </div>
        <div class="mt-3.5 flex items-center gap-3.5">
          <img src={iconUrl(best.item.icon)} alt="" class="size-12 flex-none rounded-lg border bg-field object-contain [image-rendering:pixelated]" style:border-color={`${gradeColor(best.grade)}66`} />
          <div class="min-w-0">
            <a href={itemHref(best.name)} class="display block truncate text-[23px] font-semibold text-ink hover:text-accent">{best.item.base || best.name}</a>
            <div class="mt-1 flex items-center gap-1.5 text-[12.5px] text-hint">
              <GradeBadge grade={best.grade} />{#if heroSub}<span>· {heroSub}</span>{/if}
              {#if best.tradable}
                <a href={steamUrl(best.name)} target="_blank" rel="noopener noreferrer" title="abrir no Mercado Steam" class="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted hover:border-accent-bright hover:text-ink">↗ Steam</a>
              {/if}
            </div>
          </div>
        </div>
        <div class="mt-[18px] flex flex-wrap gap-6">
          <div>
            <div class="display text-[32px] leading-none font-bold text-accent">{fmtAbbr(best.goldPer)}</div>
            <div class="mt-1 text-[10.5px] text-hint">gold / {cur === 'brl' ? 'R$' : '$'}</div>
          </div>
          <div class="border-l border-[#1e2b27] pl-6">
            <div class="display text-[32px] leading-none font-bold text-ink">{fmtAbbr(best.gold)}</div>
            <div class="mt-1 text-[10.5px] text-hint">{msgs.heroCube} · {money(best.price)}</div>
          </div>
        </div>
      </div>
      <div class="flex flex-col items-end gap-1.5 self-stretch justify-center">
        <Sparkline values={best.item.spark} width={160} height={46} color={deltaColor(best.chg24)} />
        <Delta pct={best.chg24} suffix="24h" />
      </div>
    </div>
  {/if}

  {#if total === 0}
    <p class="py-8 text-center text-sm text-muted">Nenhum item corresponde aos filtros.</p>
  {:else if ms.view === 'cards'}
    <div class="grid gap-[14px] [grid-template-columns:repeat(auto-fill,minmax(258px,1fr))]">
      {#each cardsList as r, i (r.name)}
        <ItemCard item={r.item} rank={i + 1} currency={cur} favorited={isFav(r.name)} onfav={() => toggleFav(r.name)} />
      {/each}
    </div>
    {#if total > CARDS_CAP}<p class="text-center text-xs text-hint">mostrando {CARDS_CAP} de {total} — refine os filtros ou use a tabela</p>{/if}
  {:else}
    <div class="overflow-x-auto rounded-[10px] border border-line">
      <div style:min-width={`${1418 + attrCols.length * 104}px`}>
        <div class="grid bg-surface text-xs font-semibold text-muted" style:grid-template-columns={gridTemplate}>
          {#each COLUMNS as col (col.key)}
            <button type="button" onclick={() => toggleSort(col.key)} class="flex items-center gap-1 px-2.5 py-2.5 hover:text-ink {col.align === 'right' ? 'justify-end' : 'justify-start'}">
              {msgs.cols[col.key]}{#if ms.sortKey === col.key}<span class="text-accent-bright">{ms.sortDir === 'asc' ? '▲' : '▼'}</span>{/if}
            </button>
          {/each}
          {#each attrCols as a (a)}
            <button type="button" onclick={() => toggleSort(`attr:${a}`)} class="flex items-center justify-end gap-1 px-2 py-2 hover:text-ink" title={attrLabel(a)}>
              <span class="truncate">{attrLabel(a)}</span>{#if ms.sortKey === `attr:${a}`}<span class="text-accent-bright">{ms.sortDir === 'asc' ? '▲' : '▼'}</span>{/if}
            </button>
          {/each}
        </div>
        <div class="h-[60vh] overflow-y-auto" bind:clientHeight={viewportH} onscroll={(e) => (scrollTop = e.currentTarget.scrollTop)}>
          <div style:height={`${total * ROW_H}px`} style:position="relative">
            <div style:transform={`translateY(${start * ROW_H}px)`}>
              {#each visible as r (r.name)}
                <div class="tabular grid items-center border-b border-line-soft text-[13px] hover:bg-row-hover" style:grid-template-columns={gridTemplate} style:height={`${ROW_H}px`}>
                  <div class="flex min-w-0 items-center gap-2 px-2.5">
                    <button type="button" onclick={() => toggleFav(r.name)} title="favoritar" class="flex-none leading-none {isFav(r.name) ? 'text-gold' : 'text-hint hover:text-gold'}">★</button>
                    <img src={iconUrl(r.item.icon)} alt="" loading="lazy" class="size-7 flex-none rounded object-contain [image-rendering:pixelated]" style:border={`1px solid ${gradeColor(r.grade)}55`} />
                    <a href={itemHref(r.name)} class="truncate text-ink hover:text-accent hover:underline">{r.item.base || r.name}</a>
                    {#if r.tradable}
                      <a href={steamUrl(r.name)} target="_blank" rel="noopener noreferrer" title="abrir no Mercado Steam" aria-label="abrir no Mercado Steam" class="flex-none text-hint hover:text-accent">↗</a>
                    {/if}
                  </div>
                  <div class="px-2"><GradeBadge grade={r.grade} /></div>
                  <div class="truncate px-2 text-muted">{r.gearType || '—'}</div>
                  <div class="truncate px-2 text-muted">{r.classes.length ? r.classes.join(', ') : '—'}</div>
                  <div class="px-2 text-right text-muted">{r.level ?? '—'}</div>
                  <div class="px-2 text-right text-gold">{fmtAbbr(r.gold)}</div>
                  <div class="px-2 text-right text-ink">{money(r.price)}</div>
                  <div class="flex justify-end px-2"><Delta pct={r.chg24} /></div>
                  <div class="relative flex items-center justify-end overflow-hidden px-2.5 font-semibold text-accent">
                    <span class="absolute inset-y-[7px] left-0 rounded-r-sm" style:width={`${Math.round(((r.goldPer ?? 0) / maxGoldPer) * 100)}%`} style:background="linear-gradient(90deg,#2dd4a733,#2dd4a70d)"></span>
                    <span class="relative">{fmtAbbr(r.goldPer)}</span>
                  </div>
                  <div class="px-2 text-right text-muted">{r.listings}</div>
                  <div class="px-2 text-right text-muted">{r.vol != null ? fmtAbbr(r.vol) : '—'}</div>
                  <div class="px-2 text-right text-ink">{fmtPrice(r.buyMax, BOOK_CUR)}</div>
                  <div class="px-2 text-right text-[#5fd38d]">{fmtPrice(r.buyNet, BOOK_CUR)}</div>
                  <div class="px-2 text-right text-muted">{r.buyOrders || '—'}</div>
                  {#each attrCols as a (a)}
                    <div class="truncate px-2 text-right text-ink" title={r.item.attrs?.[a]?.disp ?? ''}>{r.item.attrs?.[a]?.disp ?? '—'}</div>
                  {/each}
                </div>
              {/each}
            </div>
          </div>
        </div>
      </div>
    </div>
  {/if}
</div>
