<script lang="ts">
  import type { Stage } from '../../lib/contract/index.ts';
  import { fmtPrice, iconUrl } from '../../lib/format.ts';
  import { gradeColor } from '../../lib/grades.ts';

  // Card de estágio (porta de stageCard). EV = valor tradável esperado por caixa.
  interface Props {
    stage: Stage;
    /** Mapa nome do item -> preço USD de mercado (p/ anotar drops tradáveis). */
    prices?: Record<string, number | null | undefined>;
  }
  let { stage: s, prices = {} }: Props = $props();

  const money = (v: number | null | undefined) => fmtPrice(v, 'usd');
  const top = $derived(s.top ?? []);
</script>

<article class="rounded-[10px] border border-white/10 bg-white/[0.04] p-3">
  <header class="flex flex-wrap items-center gap-2">
    <span class="font-semibold text-ink">{s.label}</span>
    {#if s.level != null}<span class="text-xs text-muted">Lv {s.level}</span>{/if}
    <span class="truncate text-xs text-muted">{s.name}</span>
    {#if s.ev > 0}
      <span class="tabular ml-auto rounded-md border border-line bg-field px-1.5 py-0.5 text-[11px] text-accent" title="valor tradável esperado por caixa (aprox.)">
        ≈ {money(s.ev)}/caixa
      </span>
    {:else if s.boss}
      <span class="ml-auto text-[11px] text-hint">boss: {s.boss}</span>
    {/if}
  </header>

  {#if top.length}
    <div class="mt-2 text-[11px] text-muted">
      vale por: {#each top as [n], i (n)}<span style:color="#cdd3e0">{n}</span>{#if i < top.length - 1} · {/if}{/each}
    </div>
  {/if}

  <ul class="mt-2 space-y-0.5 border-t border-white/10 pt-2">
    {#each s.drops ?? [] as d (d.name)}
      {@const price = prices[d.name]}
      <li class="flex items-center gap-2 text-xs">
        {#if iconUrl(d.icon)}<img src={iconUrl(d.icon)} alt="" loading="lazy" class="size-4 flex-none object-contain [image-rendering:pixelated]" />{/if}
        <span class="min-w-0 flex-1 truncate" style:color={gradeColor(d.grade)}>{d.name}</span>
        {#if price != null}<span class="tabular text-accent">{money(price)}</span>{/if}
        <span class="tabular w-16 text-right text-hint">taxa {d.rate ?? '—'}</span>
      </li>
    {/each}
  </ul>
</article>
