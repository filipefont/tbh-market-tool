<script lang="ts">
  import { type Currency, currencySymbol, delta24, fmtAbbr, fmtPrice, iconUrl, priceOf } from '../../lib/format.ts';
  import { gradeColor } from '../../lib/grades.ts';
  import type { MarketLike } from '../../lib/market.ts';
  import Badge from './Badge.svelte';
  import Delta from './Delta.svelte';
  import GradeBadge from './GradeBadge.svelte';

  // Card de item do Cubo (portado de cuboCardHtml -> Tailwind + contrato).
  interface Props {
    item: MarketLike;
    rank?: number;
    currency?: Currency;
  }
  let { item, rank, currency = 'brl' }: Props = $props();

  const color = $derived(gradeColor(item.grade));
  const price = $derived(priceOf(item, currency));
  // gold por unidade de moeda — a métrica-chave do ranking (quanto gold por R$/$).
  const goldPer = $derived(price && price > 0 ? Math.round(item.gold / price) : null);
  // liquidez simples por nº de ofertas (verde/âmbar/vermelho).
  const liq = $derived(item.listings >= 20 ? '#5fd38d' : item.listings >= 5 ? '#f4c430' : '#e07a7a');
</script>

<article
  class="rounded-[10px] border border-white/10 bg-white/[0.04] p-3 transition-colors hover:bg-white/[0.07]"
  style:border-left={`3px solid ${color}`}
>
  <header class="flex items-center gap-2">
    <img
      src={iconUrl(item.icon)}
      alt=""
      loading="lazy"
      class="size-9 flex-none rounded-md border border-line bg-field object-contain [image-rendering:pixelated]"
      style:border-color={`${color}66`}
    />
    <div class="min-w-0 flex-1">
      <div class="truncate text-sm font-semibold text-ink">{item.name}</div>
      <div class="mt-0.5 flex items-center gap-1.5">
        <GradeBadge grade={item.grade} />
        {#if item.gradeLock}<Badge variant="lock" title="intradável pela trava de grade">🔒</Badge>{/if}
        {#if !item.tradable}<Badge variant="lock">intradável</Badge>{/if}
      </div>
    </div>
    {#if rank != null}
      <div class="flex flex-col items-end gap-1">
        <span class="tabular text-xs text-hint">#{rank}</span>
        <span class="inline-block size-2 rounded-full" style:background={liq} title="liquidez"></span>
      </div>
    {/if}
  </header>

  <div class="mt-3 flex items-end justify-between">
    <div>
      <div class="tabular text-xl font-semibold text-accent">{fmtAbbr(goldPer)}</div>
      <div class="text-[10px] text-muted">gold / {currencySymbol(currency)}</div>
    </div>
    <Delta pct={delta24(item)} />
  </div>

  <footer class="mt-2 flex items-center justify-between border-t border-white/10 pt-2 text-xs text-muted">
    <span>Gold <b class="tabular text-ink" title={`${item.gold} gold`}>{fmtAbbr(item.gold)}</b></span>
    <span>Preço <b class="tabular text-ink">{fmtPrice(price, currency)}</b></span>
  </footer>
</article>
