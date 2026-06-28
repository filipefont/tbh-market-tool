<script lang="ts">
  import { type Currency, fmtAbbr, fmtPrice, iconUrl } from '../../lib/format.ts';
  import { GRADE_ORDER, gradeColor } from '../../lib/grades.ts';
  import { attrLabel } from '../../lib/labels.ts';
  import { type MarketLike, type MarketRow, deriveRows } from '../../lib/market.ts';
  import { slugify } from '../../lib/slug.ts';
  import CurrencyToggle from '../ds/CurrencyToggle.svelte';
  import Delta from '../ds/Delta.svelte';
  import GradeBadge from '../ds/GradeBadge.svelte';
  import ItemCard from '../ds/ItemCard.svelte';
  import MultiSelect from '../ds/MultiSelect.svelte';

  // Tabela densa do Mercado: ordenação, busca, filtros facetados MULTI-seleção
  // (categoria/tipo/classe/grade), ranges (nível/gold/preço), só tradável/encomenda,
  // favoritos (⭐, localStorage), top movers e toggle tabela/cartões. Estado na URL.
  interface Msgs {
    search: string;
    allGrades: string;
    items: string;
    allTypes: string;
    allGearTypes: string;
    allClasses: string;
    allAttrs: string;
    lvlMin: string;
    lvlMax: string;
    goldMin: string;
    goldMax: string;
    priceMin: string;
    priceMax: string;
    tradable: string;
    withOrders: string;
    onlyFav: string;
    fav: string;
    clear: string;
    viewTable: string;
    viewCards: string;
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
  // 1ª coluna inclui a estrela (favorito)
  const GRID =
    'minmax(180px,2fr) 96px 84px 80px 50px 74px 86px 70px 96px 56px 64px 88px 88px 72px';
  const ROW_H = 36;
  const OVERSCAN = 8;
  const CARDS_CAP = 120;

  const base = import.meta.env.BASE_URL.replace(/\/$/, '');
  const itemHref = (name: string) => `${base}/item/${slugify(name)}`;
  const titleCase = (s: string) => s.charAt(0) + s.slice(1).toLowerCase();
  const csv = (v: string | null) => (v ? v.split(',').filter(Boolean) : []);
  const num = (v: string) => (v === '' ? null : Number(v));

  // ---- estado (inicializa da URL) ----------------------------------------------------
  const p0 = typeof location !== 'undefined' ? new URLSearchParams(location.search) : new URLSearchParams();
  let q = $state(p0.get('q') ?? '');
  let grade = $state<string[]>(csv(p0.get('grade')));
  let typeF = $state<string[]>(csv(p0.get('type')));
  let gtype = $state<string[]>(csv(p0.get('gt')));
  let cls = $state<string[]>(csv(p0.get('cls')));
  let attrSel = $state<string[]>(csv(p0.get('attr')));
  let lvlMin = $state(p0.get('lmin') ?? '');
  let lvlMax = $state(p0.get('lmax') ?? '');
  let goldMin = $state(p0.get('gmin') ?? '');
  let goldMax = $state(p0.get('gmax') ?? '');
  let priceMin = $state(p0.get('pmin') ?? '');
  let priceMax = $state(p0.get('pmax') ?? '');
  let onlyTrad = $state(p0.get('trad') === '1');
  let onlyBook = $state(p0.get('book') === '1');
  let onlyFav = $state(p0.get('fav') === '1');
  let cur = $state<Currency>(p0.get('cur') === 'usd' ? 'usd' : 'brl');
  let sortKey = $state<string>(p0.get('sort') || 'goldPer');
  let sortDir = $state<'asc' | 'desc'>(p0.get('dir') === 'asc' ? 'asc' : 'desc');
  let view = $state<'table' | 'cards'>(p0.get('view') === 'table' ? 'table' : 'cards');
  let scrollTop = $state(0);
  let viewportH = $state(600);

  // favoritos persistidos (localStorage)
  const FAV_KEY = 'tbh:favs';
  let favs = $state<string[]>(
    typeof localStorage !== 'undefined' ? JSON.parse(localStorage.getItem(FAV_KEY) || '[]') : [],
  );
  const isFav = (name: string) => favs.includes(name);
  function toggleFav(name: string) {
    favs = isFav(name) ? favs.filter((n) => n !== name) : [...favs, name];
    if (typeof localStorage !== 'undefined') localStorage.setItem(FAV_KEY, JSON.stringify(favs));
  }

  // opções presentes nos dados
  const presentGrades = $derived([...GRADE_ORDER].reverse().filter((g) => items.some((it) => it.grade === g)));
  const presentTypes = $derived([...new Set(items.map((it) => it.type).filter(Boolean))].sort() as string[]);
  const presentGearTypes = $derived([...new Set(items.map((it) => it.gearType).filter(Boolean))].sort() as string[]);
  const presentClasses = $derived(
    [...new Set(items.flatMap((it) => it.classes ?? []))].filter((c) => c && c !== 'All').sort(),
  );
  const presentAttrs = $derived(
    [...new Set(items.flatMap((it) => Object.keys(it.attrs ?? {})))].sort((a, b) => attrLabel(a).localeCompare(attrLabel(b))),
  );
  // colunas dinâmicas = atributos selecionados (também viram colunas, como no legado)
  const gridTemplate = $derived(GRID + attrSel.map(() => ' 104px').join(''));

  // ---- derivação: filtra -> ordena ---------------------------------------------------
  const rows = $derived(deriveRows(items, cur));
  const filtered = $derived.by(() => {
    const ql = q.toLowerCase();
    const lmin = num(lvlMin), lmax = num(lvlMax);
    const gmin = num(goldMin), gmax = num(goldMax);
    const pmin = num(priceMin), pmax = num(priceMax);
    return rows.filter((r) => {
      if (ql && !r.name.toLowerCase().includes(ql)) return false;
      if (grade.length && !grade.includes(r.grade)) return false;
      if (typeF.length && !typeF.includes(r.type)) return false;
      if (gtype.length && !gtype.includes(r.gearType)) return false;
      if (cls.length && !cls.some((c) => r.classes.includes(c))) return false;
      if (attrSel.length && !attrSel.every((a) => r.item.attrs && a in r.item.attrs)) return false;
      if (lmin != null && !(r.level != null && r.level >= lmin)) return false;
      if (lmax != null && !(r.level != null && r.level <= lmax)) return false;
      if (gmin != null && r.gold < gmin) return false;
      if (gmax != null && r.gold > gmax) return false;
      if (pmin != null && !(r.price != null && r.price >= pmin)) return false;
      if (pmax != null && !(r.price != null && r.price <= pmax)) return false;
      if (onlyTrad && !r.tradable) return false;
      if (onlyBook && r.buyMax == null) return false;
      if (onlyFav && !isFav(r.name)) return false;
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
  const cardsList = $derived(sorted.slice(0, CARDS_CAP));

  function toggleSort(key: string) {
    if (sortKey === key) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    else {
      sortKey = key;
      sortDir = key === 'name' || key === 'gearType' || key === 'classes' ? 'asc' : 'desc';
    }
  }
  function clearFilters() {
    q = lvlMin = lvlMax = goldMin = goldMax = priceMin = priceMax = '';
    grade = typeF = gtype = cls = attrSel = [];
    onlyTrad = onlyBook = onlyFav = false;
  }
  const hasFilters = $derived(
    !!(q || grade.length || typeF.length || gtype.length || cls.length || attrSel.length || lvlMin || lvlMax ||
      goldMin || goldMax || priceMin || priceMax || onlyTrad || onlyBook || onlyFav),
  );

  // ---- estado na URL -----------------------------------------------------------------
  $effect(() => {
    const p = new URLSearchParams();
    if (q) p.set('q', q);
    if (grade.length) p.set('grade', grade.join(','));
    if (typeF.length) p.set('type', typeF.join(','));
    if (gtype.length) p.set('gt', gtype.join(','));
    if (cls.length) p.set('cls', cls.join(','));
    if (attrSel.length) p.set('attr', attrSel.join(','));
    if (lvlMin) p.set('lmin', lvlMin);
    if (lvlMax) p.set('lmax', lvlMax);
    if (goldMin) p.set('gmin', goldMin);
    if (goldMax) p.set('gmax', goldMax);
    if (priceMin) p.set('pmin', priceMin);
    if (priceMax) p.set('pmax', priceMax);
    if (onlyTrad) p.set('trad', '1');
    if (onlyBook) p.set('book', '1');
    if (onlyFav) p.set('fav', '1');
    if (cur !== 'brl') p.set('cur', cur);
    if (sortKey !== 'goldPer') p.set('sort', sortKey);
    if (sortDir !== 'desc') p.set('dir', sortDir);
    if (view !== 'cards') p.set('view', view);
    const qs = p.toString();
    history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
  });

  const money = (v: number | null) => fmtPrice(v, cur);
  const fld = 'rounded-md border border-line bg-field px-2 py-1.5 text-xs text-ink focus-visible:outline-2 focus-visible:outline-accent-bright';
</script>

<div class="space-y-3">
  <!-- controles -->
  <div class="flex flex-wrap items-center gap-2">
    <input type="search" bind:value={q} placeholder={msgs.search} class="w-44 {fld}" />
    <MultiSelect label={msgs.allGrades} options={presentGrades} selected={grade} onchange={(v) => (grade = v)} fmt={titleCase} />
    <MultiSelect label={msgs.allTypes} options={presentTypes} selected={typeF} onchange={(v) => (typeF = v)} fmt={titleCase} />
    <MultiSelect label={msgs.allGearTypes} options={presentGearTypes} selected={gtype} onchange={(v) => (gtype = v)} fmt={titleCase} />
    <MultiSelect label={msgs.allClasses} options={presentClasses} selected={cls} onchange={(v) => (cls = v)} />
    <MultiSelect label={msgs.allAttrs} options={presentAttrs} selected={attrSel} onchange={(v) => (attrSel = v)} fmt={attrLabel} />
    <input type="number" bind:value={lvlMin} placeholder={msgs.lvlMin} class="w-20 {fld}" min="0" />
    <input type="number" bind:value={lvlMax} placeholder={msgs.lvlMax} class="w-20 {fld}" min="0" />
    <input type="number" bind:value={goldMin} placeholder={msgs.goldMin} class="w-24 {fld}" min="0" />
    <input type="number" bind:value={goldMax} placeholder={msgs.goldMax} class="w-24 {fld}" min="0" />
    <input type="number" bind:value={priceMin} placeholder={msgs.priceMin} class="w-24 {fld}" min="0" step="0.01" />
    <input type="number" bind:value={priceMax} placeholder={msgs.priceMax} class="w-24 {fld}" min="0" step="0.01" />
    <label class="flex items-center gap-1 text-xs text-muted"><input type="checkbox" bind:checked={onlyTrad} /> {msgs.tradable}</label>
    <label class="flex items-center gap-1 text-xs text-muted"><input type="checkbox" bind:checked={onlyBook} /> {msgs.withOrders}</label>
    <label class="flex items-center gap-1 text-xs text-muted"><input type="checkbox" bind:checked={onlyFav} /> {msgs.onlyFav}</label>
    {#if hasFilters}
      <button type="button" onclick={clearFilters} class="{fld} text-muted hover:text-ink">✕ {msgs.clear}</button>
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
        <a href={itemHref(m.name)} class="tabular inline-flex items-center gap-1 rounded-full border border-line bg-field px-2 py-0.5 text-[11px] hover:border-accent-bright" style:color={(m.chg24 ?? 0) > 0 ? '#5fd38d' : '#e07a7a'} title={m.name}>
          {(m.chg24 ?? 0) > 0 ? '▲' : '▼'} <span class="max-w-28 truncate">{m.name}</span> <b>{(m.chg24 ?? 0) > 0 ? '+' : ''}{m.chg24}%</b>
        </a>
      {/each}
    </div>
  {/if}

  <!-- hero: melhor negócio (fiel a .cubohero) -->
  {#if best}
    <div
      class="grid grid-cols-[1fr_auto] items-center gap-[18px] rounded-2xl border border-[#1e2b27] p-[20px_22px]"
      style:background="linear-gradient(120deg,#11201b,#0e1318 72%)"
    >
      <div class="min-w-0">
        <div class="flex flex-wrap items-center gap-2">
          <span class="rounded-md bg-gold px-2 py-[3px] text-[11px] font-bold text-[#1c1404]">★ TOP</span>
          <span class="rounded-full bg-accent px-2.5 py-[3px] text-[11px] font-bold text-accent-ink">{msgs.heroDeal}</span>
        </div>
        <div class="mt-3.5 flex items-center gap-3.5">
          <img src={iconUrl(best.item.icon)} alt="" class="size-12 flex-none rounded-lg border bg-field object-contain [image-rendering:pixelated]" style:border-color={`${gradeColor(best.grade)}66`} />
          <div class="min-w-0">
            <a href={itemHref(best.name)} class="display block truncate text-[23px] font-semibold text-ink hover:text-accent">{best.name}</a>
            <div class="mt-1 flex items-center gap-1.5 text-[12.5px]"><GradeBadge grade={best.grade} /></div>
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
      <div class="self-end"><Delta pct={best.chg24} suffix="24h" /></div>
    </div>
  {/if}

  {#if total === 0}
    <p class="py-8 text-center text-sm text-muted">Nenhum item corresponde aos filtros.</p>
  {:else if view === 'cards'}
    <div class="grid gap-[14px] [grid-template-columns:repeat(auto-fill,minmax(258px,1fr))]">
      {#each cardsList as r, i (r.name)}
        <ItemCard item={r.item} rank={i + 1} currency={cur} favorited={isFav(r.name)} onfav={() => toggleFav(r.name)} />
      {/each}
    </div>
    {#if total > CARDS_CAP}<p class="text-center text-xs text-hint">mostrando {CARDS_CAP} de {total} — refine os filtros ou use a tabela</p>{/if}
  {:else}
    <div class="overflow-x-auto rounded-[10px] border border-line">
      <div style:min-width={`${1120 + attrSel.length * 104}px`}>
        <div class="grid bg-surface text-[11px] font-semibold text-muted" style:grid-template-columns={gridTemplate}>
          {#each COLUMNS as col (col.key)}
            <button type="button" onclick={() => toggleSort(col.key)} class="flex items-center gap-1 px-2 py-2 hover:text-ink {col.align === 'right' ? 'justify-end' : 'justify-start'}">
              {msgs.cols[col.key]}{#if sortKey === col.key}<span class="text-accent-bright">{sortDir === 'asc' ? '▲' : '▼'}</span>{/if}
            </button>
          {/each}
          {#each attrSel as a (a)}
            <button type="button" onclick={() => toggleSort(`attr:${a}`)} class="flex items-center justify-end gap-1 px-2 py-2 hover:text-ink" title={attrLabel(a)}>
              <span class="truncate">{attrLabel(a)}</span>{#if sortKey === `attr:${a}`}<span class="text-accent-bright">{sortDir === 'asc' ? '▲' : '▼'}</span>{/if}
            </button>
          {/each}
        </div>
        <div class="h-[60vh] overflow-y-auto" bind:clientHeight={viewportH} onscroll={(e) => (scrollTop = e.currentTarget.scrollTop)}>
          <div style:height={`${total * ROW_H}px`} style:position="relative">
            <div style:transform={`translateY(${start * ROW_H}px)`}>
              {#each visible as r (r.name)}
                <div class="tabular grid items-center border-b border-line-soft text-xs hover:bg-row-hover" style:grid-template-columns={gridTemplate} style:height={`${ROW_H}px`}>
                  <div class="flex min-w-0 items-center gap-1.5 px-2">
                    <button type="button" onclick={() => toggleFav(r.name)} title={msgs.fav} class="flex-none leading-none {isFav(r.name) ? 'text-gold' : 'text-hint hover:text-gold'}">★</button>
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
                  {#each attrSel as a (a)}
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
