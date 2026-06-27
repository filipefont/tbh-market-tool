<script lang="ts">
  import type { Grade } from '../../lib/contract/index.ts';
  import { gradeColor } from '../../lib/grades.ts';

  interface Props {
    grade: Grade | string;
    /** Mostra um pontinho da cor em vez do texto completo. */
    dot?: boolean;
  }
  let { grade, dot = false }: Props = $props();
  const color = $derived(gradeColor(grade));
  const label = $derived(String(grade).charAt(0) + String(grade).slice(1).toLowerCase());
</script>

{#if dot}
  <span class="inline-flex items-center gap-1.5 text-xs text-muted">
    <i class="inline-block size-2.5 rounded-[3px]" style:background={color}></i>{label}
  </span>
{:else}
  <span
    class="inline-block rounded-md border px-1.5 py-px text-[10px] font-semibold whitespace-nowrap"
    style:color={color}
    style:border-color={`${color}55`}
    style:background={`${color}1a`}
  >
    {label}
  </span>
{/if}
