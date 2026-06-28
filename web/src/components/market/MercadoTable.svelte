<script lang="ts">
  import { type Currency, fmtAbbr, fmtPrice, iconUrl } from '../../lib/format.ts';
  import { GRADE_ORDER, gradeColor } from '../../lib/grades.ts';
  import { type MarketLike, type MarketRow, deriveRows } from '../../lib/market.ts';
  import { slugify } from '../../lib/slug.ts';
  import CurrencyToggle from '../ds/CurrencyToggle.svelte';
  import Delta from '../ds/Delta.svelte';
  import GradeBadge from '../ds/GradeBadge.svelte';
  import ItemCard from '../ds/ItemCard.svelte';

  // Tabela densa do Mercado (Fase 3 + paridade): ordenação, busca, filtros facetados
  // (categoria/tipo/classe/nível/tradável/encomenda), top movers, toggle tabela/cartões.
  // Estado na URL; virtualização manual (windowing) na tabela.
  interface Msgs {
    search: string;
    allGrades: string;
    items: string;
    allTypes: string;
    allGearTypes: string;
    allClasses: string;
    lvlMin: string;
    lvlMax: string;
    tradable: string;
    withOrders: string;
    clear: string;
    viewTable: string;
    viewCards: string;
    movers: string;
    cols: Record<SortKey, string>;
  }
  interface Props {
    items: MarketLike[];
    msgs: Msgs;
  }
  let { items, msgs }: Props = $props();

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
  const GRID =
    'minmax(170px,2fr) 96px 84px 80px 50px 74px 86px 70px 96px 56px 64px 88px 88px 72px';
  const ROW_H = 36;
  const OVERSCAN = 8;
  const CARDS_CAP = 120;

  const base = import.meta.env.BASE_URL.replace(/\/$/, '');
  const itemHref = (name: string) => `${base}/item/${slugify(name)}`;

  // ---- estado (inicializa da URL) ----------------------------------------------------
  const p0 = typeof location !== 'undefined' ? new URLSearchParams(location.search) : new URLSearchParams();
  let q = $state(p0.get('q') ?? '');
  let grade = $state(p0.get('grade') ?? '');
  let typeF = $state(p0.get('type') ?? '');
  let gtype = $state(p0.get('gt') ?? '');
  let cls = $state(p0.get('cls') ?? '');
  let lvlMin = $state(p0.get('lmin') ?? '');
  let lvlMax = $state(p0.get('lmax') ?? '');
  let onlyTrad = $state(p0.get('trad') === '1');
  let onlyBook = $state(p0.get('book') === '1');
  let cur = $state<Currency>(p0.get('cur') === 'usd' ? 'usd' : 'brl');
  let sortKey = $state<SortKey>((p0.get('sort') as SortKey) || 'goldPer');
  let sortDir = $state<'asc' | 'desc'>(p0.get('dir') === 'asc' ? 'asc' : 'desc');
  let view = $state<'table' | 'cards'>(p0.get('view') === 'cards' ? 'cards' : 'table');
  let scrollTop = $state(0);
  let viewportH = $state(600);

  // opções presentes nos dados
  const presentGrades = $derived([...GRADE_ORDER].reverse().filter((g) => items.some((it) => it.grade === g)));
  const presentTypes = $derived([...new Set(items.map((it) => it.type).filter(Boolean))].sort() as string[]);
  const presentGearTypes = $derived([...new Set(items.map((it) => it.gearType).filter(Boolean))].sort() as string[]);
  const presentClasses = $derived(
    [...new Set(items.flatMap((it) => it.classes ?? []))].filter((c) => c && c !== 'All').sort(),
  );

  const titleCase = (s: string) => s.charAt(0) + s.slice(1).toLowerCase();

  // ---- derivação: filtra -> ordena ---------------------------------------------------
  const rows = $derived(deriveRows(items, cur));
  const filtered = $derived.by(() => {
    const ql = q.toLowerCase();
    const lmin = lvlMin === '' ? null : Number(lvlMin);
    const lmax = lvlMax === '' ? null : Number(lvlMax);
    return rows.filter(
      (r) =>
        (!ql || r.name.toLowerCase().includes(ql)) &&
        (!grade || r.grade === grade) &&
        (!typeF || r.type === typeF) &&
        (!gtype || r.gearType === gtype) &&
        (!cls || r.classes.includes(cls)) &&
        (lmin == null || (r.level != null && r.level >= lmin)) &&
        (lmax == null || (r.level != null && r.level <= lmax)) &&
        (!onlyTrad || r.tradable) &&
        (!onlyBook || r.buyMax != null),
    );
  });
  function sortValue(r: MarketRow, key: SortKey): number | string | null {
    if (key === 'grade') return r.gradeRank;
    if (key === 'classes') return r.classes.join(',');
    return r[key];
  }
  const sorted = $derived.by(() => {
    const dir = sortDir === 'asc' ? 1 : -1;
    return [...filtered].sort((a, b) => {
      const x = sortValue(a, sortKey);
      const y = sortValue(b, sortKey);
      if (typeof x === 'string' || typeof y === 'string') return String(x).localeCompare(String(y)) * dir;
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return (x - y) * dir;
    });
  });
  const best = $derived(sorted[0]);

  // top movers: maiores altas/quedas por Δ24h (ignora filtros; lê tudo)
  const movers = $derived.by(() => {
    const withc = rows.filter((r) => r.chg24 != null);
    const up = [...withc].filter((r) => (r.chg24 ?? 0) > 0).sort((a, b) => (b.chg24 ?? 0) - (a.chg24 ?? 0)).slice(0, 5);
    const down = [...withc].filter((r) => (r.chg24 ?? 0) < 0).sort((a, b) => (a.chg24 ?? 0) - (b.chg24 ?? 0)).slice(0, 5);
    return [...up, ...down];
  });

  // ---- virtualização (tabela) --------------------------------------------------------
  const total = $derived(sorted.length);
  const start = $derived(Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN));
  const end = $derived(Math.min(total, start + Math.ceil(viewportH / ROW_H) + OVERSCAN * 2));
  const visible = $derived(sorted.slice(start, end));
  const cards = $derived(sorted.slice(0, CARDS_CAP));

  function toggleSort(key: SortKey) {
    if (sortKey === key) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    else {
      sortKey = key;
      sortDir = key === 'name' || key === 'gearType' || key === 'classes' ? 'asc' : 'desc';
    }
  }
  function clearFilters() {
    q = grade = typeF = gtype = cls = lvlMin = lvlMax = '';
    onlyTrad = onlyBook = false;
  }
  const hasFilters = $derived(!!(q || grade || typeF || gtype || cls || lvlMin || lvlMax || onlyTrad || onlyBook));

  // ---- estado na URL -----------------------------------------------------------------
  $effect(() => {
    const p = new URLSearchParams();
    if (q) p.set('q', q);
    if (grade) p.set('grade', grade);
    if (typeF) p.set('type', typeF);
    if (gtype) p.set('gt', gtype);
    if (cls) p.set('cls', cls);
    if (lvlMin) p.set('lmin', lvlMin);
    if (lvlMax) p.set('lmax', lvlMax);
    if (onlyTrad) p.set('trad', '1');
    if (onlyBook) p.set('book', '1');
    if (cur !== 'brl') p.set('cur', cur);
    if (sortKey !== 'goldPer') p.set('sort', sortKey);
    if (sortDir !== 'desc') p.set('dir', sortDir);
    if (view !== 'table') p.set('view', view);
    const qs = p.toString();
    history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
  });

  const money = (v: number | null) => fmtPrice(v, cur);
  const selCls = 'rounded-md border border-line bg-field px-2 py-1.5 text-xs text-ink focus-visible:outline-2 focus-visible:outline-accent-bright';
</script>

<div class="space-y-3">
  <!-- controles -->
  <div class="flex flex-wrap items-center gap-2">
    <input type="search" bind:value={q} placeholder={msgs.search} class="w-44 {selCls}" />
    <select bind:value={grade} class={selCls} aria-label={msgs.allGrades}>
      <option value="">{msgs.allGrades}</option>
      {#each presentGrades as g (g)}<option value={g}>{titleCase(g)}</option>{/each}
    </select>
    <select bind:value={typeF} class={selCls} aria-label={msgs.allTypes}>
      <option value="">{msgs.allTypes}</option>
      {#each presentTypes as t (t)}<option value={t}>{titleCase(t)}</option>{/each}
    </select>
    <select bind:value={gtype} class={selCls} aria-label={msgs.allGearTypes}>
      <option value="">{msgs.allGearTypes}</option>
      {#each presentGearTypes as t (t)}<option value={t}>{titleCase(t)}</option>{/each}
    </select>
    <select bind:value={cls} class={selCls} aria-label={msgs.allClasses}>
      <option value="">{msgs.allClasses}</option>
      {#each presentClasses as c (c)}<option value={c}>{c}</option>{/each}
    </select>
    <input type="number" bind:value={lvlMin} placeholder={msgs.lvlMin} class="w-20 {selCls}" min="0" />
    <input type="number" bind:value={lvlMax} placeholder={msgs.lvlMax} class="w-20 {selCls}" min="0" />
    <label class="flex items-center gap-1 text-xs text-muted"><input type="checkbox" bind:checked={onlyTrad} /> {msgs.tradable}</label>
    <label class="flex items-center gap-1 text-xs text-muted"><input type="checkbox" bind:checked={onlyBook} /> {msgs.withOrders}</label>
    {#if hasFilters}
      <button type="button" onclick={clearFilters} class="rounded-md border border-line bg-field px-2 py-1.5 text-xs text-muted hover:text-ink">✕ {msgs.clear}</button>
    {/if}

    <div class="ml-auto flex items-center gap-2">
      <div class="inline-flex overflow-hidden rounded-md border border-line" role="group">
        <button type="button" aria-pressed={view === 'table'} onclick={() => (view = 'table')} class="px-2.5 py-1.5 text-xs {view === 'table' ? 'bg-accent text-accent-ink font-semibold' : 'bg-field text-muted hover:bg-row-hover'}">{msgs.viewTable}</button>
        <button type="button" aria-pressed={view === 'cards'} onclick={() => (view = 'cards')} class="px-2.5 py-1.5 text-xs {view === 'cards' ? 'bg-accent text-accent-ink font-semibold' : 'bg-field text-muted hover:bg-row-hover'}">{msgs.viewCards}</button>
      </div>
      <CurrencyToggle value={cur} onchange={(c) => (cur = c)} />
      <span class="tabular text-xs text-muted">{total} {msgs.items}</span>
    </div>
  </div>

  <!-- top movers -->
  {#if movers.length}
    <div class="flex flex-wrap items-center gap-1.5">
      <span class="text-xs text-muted">{msgs.movers}</span>
      {#each movers as m (m.name)}
        <a
          href={itemHref(m.name)}
          class="tabular inline-flex items-center gap-1 rounded-full border border-line bg-field px-2 py-0.5 text-[11px] hover:border-accent-bright"
          style:color={(m.chg24 ?? 0) > 0 ? '#5fd38d' : '#e07a7a'}
          title={m.name}
        >
          {(m.chg24 ?? 0) > 0 ? '▲' : '▼'} <span class="max-w-28 truncate">{m.name}</span> <b>{(m.chg24 ?? 0) > 0 ? '+' : ''}{m.chg24}%</b>
        </a>
      {/each}
    </div>
  {/if}

  <!-- hero: melhor da ordenação atual -->
  {#if best}
    <div class="flex items-center gap-3 rounded-[10px] border border-white/10 bg-gold-bg/40 p-3" style:border-left={`3px solid ${gradeColor(best.grade)}`}>
      <img src={iconUrl(best.item.icon)} alt="" class="size-11 flex-none rounded-md border border-line bg-field object-contain [image-rendering:pixelated]" />
      <div class="min-w-0">
        <div class="flex items-center gap-2">
          <span class="text-[10px] font-bold text-gold">★ TOP</span>
          <a href={itemHref(best.name)} class="truncate font-semibold text-ink hover:text-accent hover:underline">{best.name}</a>
          <GradeBadge grade={best.grade} />
        </div>
        <div class="tabular mt-0.5 text-xs text-muted"><b class="text-accent">{fmtAbbr(best.goldPer)}</b> gold/{cur === 'brl' ? 'R$' : '$'} · Gold {fmtAbbr(best.gold)} · {money(best.price)}</div>
      </div>
      <div class="ml-auto"><Delta pct={best.chg24} suffix="24h" /></div>
    </div>
  {/if}

  {#if total === 0}
    <p class="py-8 text-center text-sm text-muted">Nenhum item corresponde aos filtros.</p>
  {:else if view === 'cards'}
    <!-- layout de cartões (Cubo) -->
    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {#each cards as r, i (r.name)}
        <ItemCard item={r.item} rank={i + 1} currency={cur} />
      {/each}
    </div>
    {#if total > CARDS_CAP}
      <p class="text-center text-xs text-hint">mostrando {CARDS_CAP} de {total} — refine os filtros ou use a tabela</p>
    {/if}
  {:else}
    <!-- tabela densa (grid + virtualização) -->
    <div class="overflow-x-auto rounded-[10px] border border-line">
      <div style:min-width="1100px">
        <div class="grid bg-surface text-[11px] font-semibold text-muted" style:grid-template-columns={GRID}>
          {#each COLUMNS as col (col.key)}
            <button type="button" onclick={() => toggleSort(col.key)} class="flex items-center gap-1 px-2 py-2 hover:text-ink {col.align === 'right' ? 'justify-end' : 'justify-start'}">
              {msgs.cols[col.key]}
              {#if sortKey === col.key}<span class="text-accent-bright">{sortDir === 'asc' ? '▲' : '▼'}</span>{/if}
            </button>
          {/each}
        </div>

        <div class="h-[60vh] overflow-y-auto" bind:clientHeight={viewportH} onscroll={(e) => (scrollTop = e.currentTarget.scrollTop)}>
          <div style:height={`${total * ROW_H}px`} style:position="relative">
            <div style:transform={`translateY(${start * ROW_H}px)`}>
              {#each visible as r (r.name)}
                <div class="tabular grid items-center border-b border-line-soft text-xs hover:bg-row-hover" style:grid-template-columns={GRID} style:height={`${ROW_H}px`}>
                  <div class="flex min-w-0 items-center gap-2 px-2">
                    <img src={iconUrl(r.item.icon)} alt="" loading="lazy" class="size-5 flex-none rounded object-contain [image-rendering:pixelated]" style:border={`1px solid ${gradeColor(r.grade)}55`} />
                    <a href={itemHref(r.name)} class="truncate text-ink hover:text-accent hover:underline">{r.name}</a>
                  </div>
                  <div class="px-2"><GradeBadge grade={r.grade} /></div>
                  <div class="truncate px-2 text-muted">{r.gearType || '—'}</div>
                  <div class="truncate px-2 text-muted">{r.classes.length ? r.classes.join(', ') : '—'}</div>
                  <div class="px-2 text-right text-muted">{r.level ?? '—'}</div>
                  <div class="px-2 text-right text-gold">{fmtAbbr(r.gold)}</div>
                  <div class="px-2 text-right text-ink">{money(r.price)}</div>
                  <div class="flex justify-end px-2"><Delta pct={r.chg24} /></div>
                  <div class="px-2 text-right font-semibold text-accent">{fmtAbbr(r.goldPer)}</div>
                  <div class="px-2 text-right text-muted">{r.listings}</div>
                  <div class="px-2 text-right text-muted">{r.vol != null ? fmtAbbr(r.vol) : '—'}</div>
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
  {/if}
</div>
