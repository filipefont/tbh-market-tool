<script lang="ts">
  import type { CraftRecipe } from '../../lib/contract/index.ts';
  import { fmtPrice, iconUrl } from '../../lib/format.ts';
  import { gradeColor } from '../../lib/grades.ts';
  import { typeLabel, verdictMeta } from '../../lib/labels.ts';
  import Badge from './Badge.svelte';
  import GradeBadge from './GradeBadge.svelte';

  // Card de receita de craft (porta de craftCard). Preços vêm em USD do pipeline.
  interface Props {
    recipe: CraftRecipe;
  }
  let { recipe: c }: Props = $props();

  const meta = $derived(verdictMeta(c.verdict));
  const badgeVariant = $derived(meta.variant === 'unknown' ? 'neutral' : meta.variant);
  const money = (v: number | null | undefined) => fmtPrice(v, 'usd');
  const lvl = $derived(`Lv ${c.lvl?.[0] ?? '?'}–${c.lvl?.[1] ?? '?'}`);
  const profit = $derived(c.pWin != null && c.cost != null && c.ev != null && c.ev > c.cost);
</script>

<article class="rounded-[10px] border border-white/10 bg-white/[0.04] p-3">
  <header class="flex items-center gap-2">
    <span class="font-bold text-ink">{typeLabel(c.type)}</span>
    <span class="tabular text-[11px] text-muted">T{c.tier}</span>
    <span class="tabular text-[11px] text-hint">{lvl}</span>
    <span class="ml-auto"><Badge variant={badgeVariant} title={meta.tip}>{meta.txt}</Badge></span>
  </header>

  <!-- economia: reagentes · EV revenda · chance de lucro -->
  <div class="tabular mt-2 flex flex-wrap gap-4 text-sm">
    <span class="text-muted"><span class="block text-[10px]">reagentes</span><b class="text-[#e0b07a]">{money(c.cost)}</b></span>
    <span class="text-muted"><span class="block text-[10px]">EV revenda</span><b class="text-ink">{money(c.ev)}</b></span>
    <span class="text-muted">
      <span class="block text-[10px]">chance lucro</span>
      <b class={profit ? 'text-[#5fd38d]' : 'text-ink'}>{c.pWin != null ? Math.round(c.pWin * 100) + '%' : '—'}</b>
    </span>
  </div>

  {#if c.floor != null || c.ceil != null}
    <div class="tabular mt-1 text-xs text-muted">
      faixa da pull: <span class="text-[#c2c9da]">{money(c.floor)}</span> — <span class="text-[#5fd38d]">{money(c.ceil)}</span>
    </div>
  {/if}

  <!-- reagentes -->
  <div class="mt-2 border-t border-white/10 pt-2">
    <span class="text-[10px] tracking-wide text-hint uppercase">Reagentes</span>
    <div class="mt-1 flex flex-wrap gap-1.5">
      {#each c.mats ?? [] as m (m.name)}
        <span class="inline-flex items-center gap-1 rounded-md border border-white/10 bg-white/5 px-1.5 py-0.5 text-[11px]">
          {#if iconUrl(m.icon)}<img src={iconUrl(m.icon)} alt="" loading="lazy" class="size-4 object-contain [image-rendering:pixelated]" />{/if}
          {#if (m.count ?? 0) > 1}<b>{m.count}×</b>{/if}
          <span>{m.name}</span>
          <span class="tabular {m.price != null ? 'text-accent' : 'text-[#e07a7a]'}">{m.price != null ? money(m.price) : 's/preço'}</span>
        </span>
      {/each}
    </div>
  </div>

  <!-- pull: melhor item por grade -->
  <div class="mt-2 border-t border-white/10 pt-2">
    <span class="text-[10px] tracking-wide text-hint uppercase">Pull (melhor por grade)</span>
    <ul class="mt-1 space-y-0.5">
      {#each c.grades ?? [] as g (g.grade)}
        <li class="flex items-center gap-2 text-xs" class:opacity-50={!g.best}>
          <span class="tabular w-10 text-right text-hint">{g.pct != null ? g.pct + '%' : ''}</span>
          <GradeBadge grade={g.grade} />
          {#if g.best}
            <span class="flex-1 truncate" style:color={gradeColor(g.grade)}>{g.best.name}</span>
            <span class="tabular whitespace-nowrap {c.cost != null && g.best.price != null && g.best.price > c.cost ? 'text-[#5fd38d] font-semibold' : 'text-accent'}">{money(g.best.price)}</span>
          {:else}
            <span class="flex-1 truncate text-muted">{g.ntot} {g.ntot > 1 ? 'itens' : 'item'}</span>
            <span class="text-[11px] text-hint">sem oferta</span>
          {/if}
        </li>
      {/each}
    </ul>
  </div>
</article>
