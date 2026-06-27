<script lang="ts">
  import { deltaColor, fmtDelta, trendOf } from '../../lib/format.ts';

  interface Props {
    /** Variação em % (ex.: chg24 do item). */
    pct: number | null | undefined;
    /** Sufixo opcional, ex.: "24h". */
    suffix?: string;
  }
  let { pct, suffix }: Props = $props();
  const color = $derived(deltaColor(pct));
  const arrow = $derived(trendOf(pct) === 'up' ? '▲' : trendOf(pct) === 'down' ? '▼' : '·');
</script>

<span class="tabular inline-flex items-center gap-1 text-xs font-semibold" style:color>
  <span aria-hidden="true">{arrow}</span>{fmtDelta(pct)}{#if suffix}<span class="text-hint font-normal">· {suffix}</span>{/if}
</span>
