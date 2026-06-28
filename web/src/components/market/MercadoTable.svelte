<script lang="ts">
  import { type Currency, fmtAbbr, fmtPrice, iconUrl } from '../../lib/format.ts';
  import { GRADE_ORDER, gradeColor } from '../../lib/grades.ts';
  import { type MarketLike, type MarketRow, deriveRows } from '../../lib/market.ts';
  import { slugify } from '../../lib/slug.ts';
  import CurrencyToggle from '../ds/CurrencyToggle.svelte';
  import Delta from '../ds/Delta.svelte';
  import GradeBadge from '../ds/GradeBadge.svelte';

  // Tabela densa do Mercado (Fase 3). Ordenação/filtro/busca com runes; estado na
  // URL; virtualização manual (windowing de altura fixa) p/ as ~900 linhas.
  interface Props {
    items: MarketLike[];
  }
  let { items }: Props = $props();

  type SortKey =
    | 'name' | 'grade' | 'gearType' | 'level' | 'gold' | 'price'
    | 'chg24' | 'goldPer' | 'listings' | 'buyMax' | 'buyNet' | 'buyOrders';

  const COLUMNS: { key: SortKey; label: string; align: 'left' | 'right' }[] = [
    { key: 'name', label: 'Item', align: 'left' },
    { key: 'grade', label: 'Grade', align: 'left' },
    { key: 'gearType', label: 'Tipo', align: 'left' },
    { key: 'level', label: 'Lvl', align: 'right' },
    { key: 'gold', label: 'Gold', align: 'right' },
    { key: 'price', label: 'Preço', align: 'right' },
    { key: 'chg24', label: 'Δ24h', align: 'right' },
    { key: 'goldPer', label: 'Gold/moeda', align: 'right' },
    { key: 'listings', label: 'List.', align: 'right' },
    { key: 'buyMax', label: 'Maior enc.', align: 'right' },
    { key: 'buyNet', label: 'Líquido', align: 'right' },
    { key: 'buyOrders', label: 'Encom.', align: 'right' },
  ];
  const GRID = 'minmax(180px,2fr) 96px 90px 52px 78px 92px 74px 104px 60px 96px 96px 78px';
  const ROW_H = 36;
  const OVERSCAN = 8;

  // ---- estado (inicializa da URL) ----------------------------------------------------
  const params = typeof location !== 'undefined' ? new URLSearchParams(location.search) : new URLSearchParams();
  let q = $state(params.get('q') ?? '');
  let gradeFilter = $state(params.get('grade') ?? '');
  let cur = $state<Currency>(params.get('cur') === 'usd' ? 'usd' : 'brl');
  let sortKey = $state<SortKey>((params.get('sort') as SortKey) || 'goldPer');
  let sortDir = $state<'asc' | 'desc'>(params.get('dir') === 'asc' ? 'asc' : 'desc');
  let scrollTop = $state(0);
  let viewportH = $state(600);

  // grades presentes (rara -> comum) p/ o filtro
  const presentGrades = $derived(
    [...GRADE_ORDER].reverse().filter((g) => items.some((it) => it.grade === g)),
  );

  // ---- derivação: filtra -> ordena ---------------------------------------------------
  const rows = $derived(deriveRows(items, cur));
  const filtered = $derived(
    rows.filter(
      (r) =>
        (!q || r.name.toLowerCase().includes(q.toLowerCase())) &&
        (!gradeFilter || r.grade === gradeFilter),
    ),
  );
  function sortValue(r: MarketRow, key: SortKey): number | string | null {
    if (key === 'grade') return r.gradeRank;
    return r[key];
  }
  const sorted = $derived.by(() => {
    const dir = sortDir === 'asc' ? 1 : -1;
    return [...filtered].sort((a, b) => {
      const x = sortValue(a, sortKey);
      const y = sortValue(b, sortKey);
      if (typeof x === 'string' || typeof y === 'string') {
        return String(x).localeCompare(String(y)) * dir;
      }
      // nulos sempre por último, independente da direção
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return (x - y) * dir;
    });
  });
  const best = $derived(sorted[0]);

  // ---- virtualização -----------------------------------------------------------------
  const total = $derived(sorted.length);
  const start = $derived(Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN));
  const end = $derived(Math.min(total, start + Math.ceil(viewportH / ROW_H) + OVERSCAN * 2));
  const visible = $derived(sorted.slice(start, end));

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      sortKey = key;
      sortDir = key === 'name' || key === 'gearType' ? 'asc' : 'desc';
    }
  }

  // ---- sincroniza o estado na URL (replaceState) -------------------------------------
  $effect(() => {
    const p = new URLSearchParams();
    if (q) p.set('q', q);
    if (gradeFilter) p.set('grade', gradeFilter);
    if (cur !== 'brl') p.set('cur', cur);
    if (sortKey !== 'goldPer') p.set('sort', sortKey);
    if (sortDir !== 'desc') p.set('dir', sortDir);
    const qs = p.toString();
    history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
  });

  const money = (v: number | null) => fmtPrice(v, cur);
  const base = import.meta.env.BASE_URL.replace(/\/$/, '');
</script>

<div class="space-y-4">
  <!-- controles -->
  <div class="flex flex-wrap items-center gap-3">
    <input
      type="search"
      bind:value={q}
      placeholder="buscar item…"
      class="w-48 rounded-md border border-line bg-field px-3 py-1.5 text-sm text-ink focus-visible:outline-2 focus-visible:outline-accent-bright"
    />
    <select
      bind:value={gradeFilter}
      class="rounded-md border border-line bg-field px-2 py-1.5 text-sm text-ink focus-visible:outline-2 focus-visible:outline-accent-bright"
    >
      <option value="">todas as grades</option>
      {#each presentGrades as g (g)}
        <option value={g}>{g.charAt(0) + g.slice(1).toLowerCase()}</option>
      {/each}
    </select>
    <CurrencyToggle value={cur} onchange={(c) => (cur = c)} />
    <span class="tabular ml-auto text-xs text-muted">{total} itens</span>
  </div>

  <!-- hero: melhor da ordenação atual -->
  {#if best}
    <div
      class="flex items-center gap-3 rounded-[10px] border border-white/10 bg-gold-bg/40 p-3"
      style:border-left={`3px solid ${gradeColor(best.grade)}`}
    >
      <img src={iconUrl(best.item.icon)} alt="" class="size-11 flex-none rounded-md border border-line bg-field object-contain [image-rendering:pixelated]" />
      <div class="min-w-0">
        <div class="flex items-center gap-2">
          <span class="text-[10px] font-bold text-gold">★ TOP</span>
          <span class="truncate font-semibold text-ink">{best.name}</span>
          <GradeBadge grade={best.grade} />
        </div>
        <div class="tabular mt-0.5 text-xs text-muted">
          <b class="text-accent">{fmtAbbr(best.goldPer)}</b> gold/{cur === 'brl' ? 'R$' : '$'} · Gold {fmtAbbr(best.gold)} · {money(best.price)}
        </div>
      </div>
      <div class="ml-auto"><Delta pct={best.chg24} suffix="24h" /></div>
    </div>
  {/if}

  <!-- tabela densa (grid + virtualização) -->
  <div class="overflow-x-auto rounded-[10px] border border-line">
    <div style:min-width="980px">
      <!-- cabeçalho -->
      <div class="grid bg-surface text-[11px] font-semibold text-muted" style:grid-template-columns={GRID}>
        {#each COLUMNS as col (col.key)}
          <button
            type="button"
            onclick={() => toggleSort(col.key)}
            class="flex items-center gap-1 px-2 py-2 hover:text-ink {col.align === 'right' ? 'justify-end' : 'justify-start'}"
          >
            {col.label}
            {#if sortKey === col.key}<span class="text-accent-bright">{sortDir === 'asc' ? '▲' : '▼'}</span>{/if}
          </button>
        {/each}
      </div>

      <!-- corpo virtualizado -->
      <div
        class="h-[64vh] overflow-y-auto"
        bind:clientHeight={viewportH}
        onscroll={(e) => (scrollTop = e.currentTarget.scrollTop)}
      >
        <div style:height={`${total * ROW_H}px`} style:position="relative">
          <div style:transform={`translateY(${start * ROW_H}px)`}>
            {#each visible as r (r.name)}
              <div
                class="tabular grid items-center border-b border-line-soft text-xs hover:bg-row-hover"
                style:grid-template-columns={GRID}
                style:height={`${ROW_H}px`}
              >
                <div class="flex min-w-0 items-center gap-2 px-2">
                  <img src={iconUrl(r.item.icon)} alt="" loading="lazy" class="size-5 flex-none rounded object-contain [image-rendering:pixelated]" style:border={`1px solid ${gradeColor(r.grade)}55`} />
                  <a href={`${base}/item/${slugify(r.name)}`} class="truncate text-ink hover:text-accent hover:underline">{r.name}</a>
                </div>
                <div class="px-2"><GradeBadge grade={r.grade} /></div>
                <div class="truncate px-2 text-muted">{r.gearType || '—'}</div>
                <div class="px-2 text-right text-muted">{r.level ?? '—'}</div>
                <div class="px-2 text-right text-gold">{fmtAbbr(r.gold)}</div>
                <div class="px-2 text-right text-ink">{money(r.price)}</div>
                <div class="flex justify-end px-2"><Delta pct={r.chg24} /></div>
                <div class="px-2 text-right font-semibold text-accent">{fmtAbbr(r.goldPer)}</div>
                <div class="px-2 text-right text-muted">{r.listings}</div>
                <div class="px-2 text-right text-ink">{fmtPrice(r.buyMax, 'brl')}</div>
                <div class="px-2 text-right text-[#5fd38d]">{fmtPrice(r.buyNet, 'brl')}</div>
                <div class="px-2 text-right text-muted">{r.buyOrders || '—'}</div>
              </div>
            {/each}
          </div>
        </div>
      </div>
    </div>
  </div>

  {#if total === 0}
    <p class="py-8 text-center text-sm text-muted">Nenhum item corresponde aos filtros.</p>
  {/if}
</div>
