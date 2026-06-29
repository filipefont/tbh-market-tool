<script lang="ts">
  import { type Currency, currencySymbol, delta24, deltaColor, fmtAbbr, fmtPrice, iconUrl, priceOf } from '../../lib/format.ts';
  import { gradeColor } from '../../lib/grades.ts';
  import type { MarketLike } from '../../lib/market.ts';
  import Badge from './Badge.svelte';
  import Delta from './Delta.svelte';
  import GradeBadge from './GradeBadge.svelte';
  import Sparkline from './Sparkline.svelte';

  // Card do Cubo (fiel a .cubocard): border-top da grade, nome limpo + sub
  // grade·tipo·lvl, métrica gold/moeda em Space Grotesk, sparkline, rodapé mono.
  interface Props {
    item: MarketLike;
    rank?: number;
    currency?: Currency;
    favorited?: boolean;
    onfav?: () => void;
  }
  let { item, rank, currency = 'brl', favorited, onfav }: Props = $props();

  const color = $derived(gradeColor(item.grade));
  const price = $derived(priceOf(item, currency));
  const goldPer = $derived(price && price > 0 ? Math.round(item.gold / price) : null);
  const liq = $derived(item.listings >= 20 ? '#5fd38d' : item.listings >= 5 ? '#f4c430' : '#e07a7a');
  const titleCase = (s: string) => s.charAt(0) + s.slice(1).toLowerCase();
  const sub = $derived(
    [item.gearType ? titleCase(item.gearType) : null, item.level != null ? `lvl ${item.level}` : null]
      .filter(Boolean)
      .join(' · '),
  );
</script>

<article
  class="flex flex-col gap-3 rounded-[14px] border border-line bg-surface p-[15px] transition-colors hover:border-[#2dd4a766]"
  style:border-top={`2px solid ${color}`}
>
  <div class="flex items-start gap-[11px]">
    <img
      src={iconUrl(item.icon)}
      alt=""
      loading="lazy"
      class="size-10 flex-none rounded-md border bg-field object-contain [image-rendering:pixelated]"
      style:border-color={`${color}66`}
    />
    <div class="min-w-0 flex-1">
      <div class="display truncate text-[15px] font-semibold text-ink">{item.base || item.name}</div>
      <div class="mt-1 flex items-center gap-1.5 text-[11.5px] text-hint">
        <GradeBadge grade={item.grade} />
        {#if sub}<span class="truncate">· {sub}</span>{/if}
        {#if item.gradeLock}<Badge variant="lock" title="intradável">🔒</Badge>{/if}
      </div>
    </div>
    <div class="flex flex-none flex-col items-end gap-1.5">
      {#if onfav}
        <button type="button" onclick={onfav} aria-pressed={favorited} title="favoritar" class="text-sm leading-none {favorited ? 'text-gold' : 'text-hint hover:text-gold'}">★</button>
      {/if}
      {#if rank != null}<span class="tabular text-[10.5px] text-[#6a7280]">#{rank}</span>{/if}
      <span class="inline-block size-2 rounded-[3px]" style:background={liq} title="liquidez"></span>
    </div>
  </div>

  <div class="flex items-end justify-between">
    <div>
      <div class="display text-[25px] leading-none font-bold text-accent">{fmtAbbr(goldPer)}</div>
      <div class="mt-[3px] text-[10.5px] text-hint">gold / {currencySymbol(currency)}</div>
    </div>
    <div class="flex flex-col items-end gap-1">
      <Sparkline values={item.spark} color={deltaColor(delta24(item))} />
      <Delta pct={delta24(item)} />
    </div>
  </div>

  <div class="flex justify-between border-t border-line-soft pt-[10px] text-[11.5px] text-hint">
    <span>Gold <b class="tabular text-[#cdd3dd]" title={`${item.gold} gold`}>{fmtAbbr(item.gold)}</b></span>
    <span>Preço <b class="tabular text-[#cdd3dd]">{fmtPrice(price, currency)}</b></span>
  </div>
</article>
