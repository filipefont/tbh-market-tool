// @ts-check
import { defineConfig } from 'astro/config';

import svelte from '@astrojs/svelte';
import tailwindcss from '@tailwindcss/vite';

import sitemap from '@astrojs/sitemap';

// Coexistência (Etapa 4 / Fase 0): o front legado fica em `/tbh-market-tool/`
// e o novo (Astro) é publicado em `/tbh-market-tool/next/` até o cutover.
// site+base geram links com o prefixo correto no GitHub Pages (project pages).
// https://astro.build/config
export default defineConfig({
  site: 'https://filipefont.github.io',
  base: '/tbh-market-tool/next',
  output: 'static',
  trailingSlash: 'ignore',

  integrations: [svelte(), sitemap()],

  vite: {
    plugins: [tailwindcss()]
  }
});