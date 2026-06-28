import { describe, expect, it } from 'vitest';
import { buildSlugMap, slugify } from '../src/lib/slug.ts';

describe('slug', () => {
  it('slugify normaliza nome -> url', () => {
    expect(slugify('Amber Ring (Arcana) A')).toBe('amber-ring-arcana-a');
    expect(slugify('Dimensional Helmet (Arcana) A')).toBe('dimensional-helmet-arcana-a');
    expect(slugify('  Café  Über ')).toBe('cafe-uber');
  });

  it('buildSlugMap garante unicidade em colisões', () => {
    const map = buildSlugMap(['A B', 'a-b', 'A  B']);
    const slugs = [...map.keys()];
    expect(new Set(slugs).size).toBe(3); // sufixos -2, -3
    expect(slugs[0]).toBe('a-b');
  });
});
