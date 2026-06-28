<script lang="ts">
  // Faceta multi-seleção (dropdown com checkboxes), via <details> (sem JS de
  // clique-fora). Mostra o rótulo + contagem de selecionados.
  interface Props {
    label: string;
    options: string[];
    selected: string[];
    onchange: (v: string[]) => void;
    /** rótulo exibido p/ cada opção (default: a própria opção). */
    fmt?: (v: string) => string;
  }
  let { label, options, selected, onchange, fmt = (v) => v }: Props = $props();

  function toggle(v: string) {
    const s = new Set(selected);
    if (s.has(v)) s.delete(v);
    else s.add(v);
    onchange([...s]);
  }

  // fecha o painel ao clicar fora
  let el: HTMLDetailsElement;
  $effect(() => {
    function onDoc(e: MouseEvent) {
      if (el?.open && !el.contains(e.target as Node)) el.open = false;
    }
    document.addEventListener('click', onDoc);
    return () => document.removeEventListener('click', onDoc);
  });
</script>

<details bind:this={el} class="relative">
  <summary
    class="flex cursor-pointer list-none items-center gap-1 rounded-md border border-line bg-field px-2 py-1.5 text-xs whitespace-nowrap text-ink
           {selected.length ? 'border-accent-bright' : ''}"
  >
    {label}{#if selected.length}<span class="text-accent">({selected.length})</span>{/if}
    <span class="text-hint">▾</span>
  </summary>
  <div class="absolute z-20 mt-1 max-h-64 min-w-40 overflow-auto rounded-md border border-line bg-surface p-1 shadow-lg">
    {#each options as o (o)}
      <label class="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-xs text-ink hover:bg-row-hover">
        <input type="checkbox" checked={selected.includes(o)} onchange={() => toggle(o)} />
        {fmt(o)}
      </label>
    {/each}
  </div>
</details>
