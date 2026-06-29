<script lang="ts">
  import { attrLabel } from '../../lib/labels.ts';
  import {
    attrA, clsA, goldMaxA, goldMinA, gradeA, gtypeA, lvlMaxA, lvlMinA, onlyBookA,
    onlyFavA, onlyTradA, priceMaxA, priceMinA, typeA, clearFilters, wireUrlSync,
  } from '../../lib/stores/market.ts';
  import { ms } from '../../lib/stores/marketState.svelte.ts';
  import MultiSelect from '../ds/MultiSelect.svelte';

  // Filtros do Mercado, na SIDEBAR (como no Cubo legado). Escrevem nos atoms
  // compartilhados; a tabela/cards (outro island) reagem ao mesmo estado.
  interface Msgs {
    search: string;
    allGrades: string;
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
    clear: string;
  }
  interface Props {
    options: { grades: string[]; types: string[]; gtypes: string[]; classes: string[]; attrs: string[] };
    msgs: Msgs;
  }
  let { options, msgs }: Props = $props();

  $effect(() => wireUrlSync());

  const titleCase = (s: string) => s.charAt(0) + s.slice(1).toLowerCase();
  const fld =
    'w-full rounded-md border border-line bg-field px-2 py-1.5 text-xs text-ink focus-visible:outline-2 focus-visible:outline-accent-bright';
  const hasFilters = $derived(
    !!(ms.q || ms.grade.length || ms.type.length || ms.gtype.length || ms.cls.length || ms.attr.length ||
      ms.lvlMin || ms.lvlMax || ms.goldMin || ms.goldMax || ms.priceMin || ms.priceMax ||
      ms.onlyTrad || ms.onlyBook || ms.onlyFav),
  );
</script>

<div class="space-y-2">
  <MultiSelect label={msgs.allGrades} options={options.grades} selected={ms.grade} onchange={(v) => gradeA.set(v)} fmt={titleCase} />
  <MultiSelect label={msgs.allTypes} options={options.types} selected={ms.type} onchange={(v) => typeA.set(v)} fmt={titleCase} />
  <MultiSelect label={msgs.allGearTypes} options={options.gtypes} selected={ms.gtype} onchange={(v) => gtypeA.set(v)} fmt={titleCase} />
  <MultiSelect label={msgs.allClasses} options={options.classes} selected={ms.cls} onchange={(v) => clsA.set(v)} />
  <MultiSelect label={msgs.allAttrs} options={options.attrs} selected={ms.attr} onchange={(v) => attrA.set(v)} fmt={attrLabel} />

  <div class="flex gap-2">
    <input type="number" min="0" value={ms.lvlMin} oninput={(e) => lvlMinA.set(e.currentTarget.value)} placeholder={msgs.lvlMin} class={fld} />
    <input type="number" min="0" value={ms.lvlMax} oninput={(e) => lvlMaxA.set(e.currentTarget.value)} placeholder={msgs.lvlMax} class={fld} />
  </div>
  <div class="flex gap-2">
    <input type="number" min="0" value={ms.goldMin} oninput={(e) => goldMinA.set(e.currentTarget.value)} placeholder={msgs.goldMin} class={fld} />
    <input type="number" min="0" value={ms.goldMax} oninput={(e) => goldMaxA.set(e.currentTarget.value)} placeholder={msgs.goldMax} class={fld} />
  </div>
  <div class="flex gap-2">
    <input type="number" min="0" step="0.01" value={ms.priceMin} oninput={(e) => priceMinA.set(e.currentTarget.value)} placeholder={msgs.priceMin} class={fld} />
    <input type="number" min="0" step="0.01" value={ms.priceMax} oninput={(e) => priceMaxA.set(e.currentTarget.value)} placeholder={msgs.priceMax} class={fld} />
  </div>

  <label class="flex items-center gap-2 text-xs text-muted"><input type="checkbox" checked={ms.onlyTrad} onchange={(e) => onlyTradA.set(e.currentTarget.checked)} /> {msgs.tradable}</label>
  <label class="flex items-center gap-2 text-xs text-muted"><input type="checkbox" checked={ms.onlyBook} onchange={(e) => onlyBookA.set(e.currentTarget.checked)} /> {msgs.withOrders}</label>
  <label class="flex items-center gap-2 text-xs text-muted"><input type="checkbox" checked={ms.onlyFav} onchange={(e) => onlyFavA.set(e.currentTarget.checked)} /> {msgs.onlyFav}</label>

  {#if hasFilters}
    <button type="button" onclick={clearFilters} class="{fld} text-left text-muted hover:text-ink">✕ {msgs.clear}</button>
  {/if}
</div>
