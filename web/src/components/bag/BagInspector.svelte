<script lang="ts">
  import type { MarketLike } from '../../lib/market.ts';
  import { fmtAbbr, fmtPrice, iconUrl } from '../../lib/format.ts';
  import { gradeColor } from '../../lib/grades.ts';
  import { type BagValue, decryptSave, parseSave, valueBag } from '../../lib/savefile.ts';
  import GradeBadge from '../ds/GradeBadge.svelte';

  // My Bag: lê o SaveFile_Live.es3 localmente (Web Crypto) e valora o inventário
  // pela encomenda líquida (BRL). SOMENTE LEITURA — nunca grava o arquivo.
  interface Msgs {
    select: string;
    privacy: string;
    loading: string;
    error: string;
    value: string;
    valueHint: string;
    gold: string;
    items: string;
    withPrice: string;
    noPrice: string;
    offCatalog: string;
    colQty: string;
    colUnit: string;
    colTotal: string;
  }
  interface Props {
    items: MarketLike[];
    msgs: Msgs;
  }
  let { items, msgs }: Props = $props();

  let state = $state<'idle' | 'loading' | 'done' | 'error'>('idle');
  let error = $state('');
  let bag = $state<BagValue | null>(null);

  async function onFile(e: Event) {
    const file = (e.currentTarget as HTMLInputElement).files?.[0];
    if (!file) return;
    state = 'loading';
    error = '';
    try {
      const buf = await file.arrayBuffer();
      const plain = await decryptSave(buf);
      bag = valueBag(parseSave(plain), items);
      state = 'done';
    } catch (err) {
      state = 'error';
      error = err instanceof Error ? err.message : String(err);
    }
  }
</script>

<div class="space-y-4">
  <label class="flex cursor-pointer flex-col items-center gap-2 rounded-[10px] border border-dashed border-line bg-white/[0.03] px-6 py-10 text-center hover:border-accent-bright">
    <span class="tabular text-sm font-semibold text-ink">{msgs.select}</span>
    <span class="max-w-md text-xs text-muted">{msgs.privacy}</span>
    <input type="file" accept=".es3" class="hidden" onchange={onFile} />
  </label>

  {#if state === 'loading'}
    <p class="text-sm text-muted">{msgs.loading}</p>
  {:else if state === 'error'}
    <p class="text-sm text-[#e07a7a]">{msgs.error} ({error})</p>
  {:else if state === 'done' && bag}
    <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <div class="rounded-[10px] border border-white/10 bg-white/[0.04] p-3">
        <div class="text-[10px] tracking-wide text-hint uppercase">{msgs.value}</div>
        <div class="tabular mt-1 text-xl font-semibold text-accent">{fmtPrice(bag.totalBRL, 'brl')}</div>
        <div class="text-[10px] text-hint">{msgs.valueHint}</div>
      </div>
      <div class="rounded-[10px] border border-white/10 bg-white/[0.04] p-3">
        <div class="text-[10px] tracking-wide text-hint uppercase">{msgs.gold}</div>
        <div class="tabular mt-1 text-xl font-semibold text-gold">{fmtAbbr(bag.gold)}</div>
      </div>
      <div class="rounded-[10px] border border-white/10 bg-white/[0.04] p-3">
        <div class="text-[10px] tracking-wide text-hint uppercase">{msgs.items}</div>
        <div class="tabular mt-1 text-xl font-semibold text-ink">{bag.totalItems}</div>
        <div class="text-[10px] text-hint">{bag.priced} {msgs.withPrice}</div>
      </div>
      <div class="rounded-[10px] border border-white/10 bg-white/[0.04] p-3">
        <div class="text-[10px] tracking-wide text-hint uppercase">{msgs.noPrice}</div>
        <div class="tabular mt-1 text-xl font-semibold text-muted">{bag.unpriced + bag.unmatched}</div>
        <div class="text-[10px] text-hint">{bag.unmatched} {msgs.offCatalog}</div>
      </div>
    </div>

    <div class="overflow-x-auto rounded-[10px] border border-line">
      <table class="w-full text-xs">
        <thead class="bg-surface text-muted">
          <tr>
            <th class="px-2 py-2 text-left font-semibold">Item</th>
            <th class="px-2 py-2 text-right font-semibold">{msgs.colQty}</th>
            <th class="px-2 py-2 text-right font-semibold">{msgs.colUnit}</th>
            <th class="px-2 py-2 text-right font-semibold">{msgs.colTotal}</th>
          </tr>
        </thead>
        <tbody>
          {#each bag.lines as l (l.key)}
            <tr class="border-t border-line-soft hover:bg-row-hover">
              <td class="px-2 py-1.5">
                <span class="flex items-center gap-2">
                  {#if iconUrl(l.icon)}<img src={iconUrl(l.icon)} alt="" loading="lazy" class="size-5 flex-none rounded object-contain [image-rendering:pixelated]" style:border={`1px solid ${gradeColor(l.grade)}55`} />{/if}
                  <span class="truncate text-ink">{l.name}</span>
                  <GradeBadge grade={l.grade} />
                </span>
              </td>
              <td class="tabular px-2 py-1.5 text-right text-muted">{l.count}</td>
              <td class="tabular px-2 py-1.5 text-right text-ink">{fmtPrice(l.unit, 'brl')}</td>
              <td class="tabular px-2 py-1.5 text-right font-semibold text-accent">{l.total ? fmtPrice(l.total, 'brl') : '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>
