// Slug estável p/ as URLs por item (/item/[slug]). Determinístico: o mesmo nome
// gera sempre o mesmo slug, no servidor (getStaticPaths) e no cliente (links da
// tabela). Ex.: "Amber Ring (Arcana) A" -> "amber-ring-arcana-a".
export function slugify(name: string): string {
  return name
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '') // tira acentos (diacríticos combinantes)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

/**
 * Mapa slug -> nome a partir de uma lista de nomes, resolvendo colisões com
 * sufixo numérico (raro, mas garante unicidade das rotas).
 */
export function buildSlugMap(names: string[]): Map<string, string> {
  const bySlug = new Map<string, string>();
  const taken = new Set<string>();
  for (const name of names) {
    let s = slugify(name) || 'item';
    if (taken.has(s)) {
      let i = 2;
      while (taken.has(`${s}-${i}`)) i++;
      s = `${s}-${i}`;
    }
    taken.add(s);
    bySlug.set(s, name);
  }
  return bySlug;
}
