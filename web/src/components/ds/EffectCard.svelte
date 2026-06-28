<script lang="ts">
  import type { MarketItem } from '../../lib/contract/index.ts';
  import { fmtPrice, iconUrl } from '../../lib/format.ts';
  import { gradeColor } from '../../lib/grades.ts';
  import { attrLabel } from '../../lib/labels.ts';
  import GradeBadge from './GradeBadge.svelte';

  // Card de gema/efeito (porta de gemCard). Mostra preço + lista de efeitos.
  interface Props {
    item: MarketItem;
  }
  let { item }: Props = $props();

  const color = $derived(gradeColor(item.grade));
  // gemas têm preço real em BRL; cai p/ USD estimado se não houver.
  const brl = $derived(item.real?.brl?.low ?? null);
  const priceText = $derived(brl != null ? fmtPrice(brl, 'brl') : fmtPrice(item.usd, 'usd'));
  const effects = $derived(item.effects ?? []);
</script>

<article class="rounded-[10px] border border-white/10 bg-white/[0.04] p-3" style:border-left={`3px solid ${color}`}>
  <header class="flex items-center gap-2">
    <img
      src={iconUrl(item.icon)}
      alt=""
      loading="lazy"
      class="size-7 flex-none rounded-md border bg-field object-contain [image-rendering:pixelated]"
      style:border-color={`${color}66`}
    />
    <span class="min-w-0 flex-1 truncate font-semibold" style:color>{item.name}</span>
    <GradeBadge grade={item.grade} />
  </header>

  <div class="tabular mt-2 text-sm text-muted">
    preço <b class="text-ink">{priceText}</b>
  </div>

  <div class="mt-2 border-t border-white/10 pt-2">
    <span class="text-[10px] tracking-wide text-hint uppercase">Efeitos</span>
    <ul class="mt-1 space-y-0.5">
      {#each effects as eff (eff.slot + eff.stat)}
        <li class="flex items-center justify-between gap-2 text-xs">
          <span class="truncate text-muted">
            {eff.slot} · {attrLabel(eff.stat)}{#if eff.chance != null && eff.chance < 1}<span class="text-hint"> ({Math.round(eff.chance * 100)}%)</span>{/if}
          </span>
          <span class="tabular font-semibold whitespace-nowrap text-accent">{eff.disp}</span>
        </li>
      {/each}
    </ul>
  </div>
</article>
