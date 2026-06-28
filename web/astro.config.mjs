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

  // i18n (Fase 4b): PT padrão (sem prefixo) + EN em /en. A "casca" (nav, títulos,
  // controles) é traduzida; dados do jogo (itens) seguem em 1 idioma por ora.
  i18n: {
    locales: ['pt', 'en'],
    defaultLocale: 'pt',
    routing: { prefixDefaultLocale: false },
  },

  integrations: [svelte(), sitemap()],

  vite: {
    plugins: [tailwindcss()]
  }
});