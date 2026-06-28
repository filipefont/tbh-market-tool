// @ts-check
import { defineConfig } from 'astro/config';

import svelte from '@astrojs/svelte';
import tailwindcss from '@tailwindcss/vite';

import sitemap from '@astrojs/sitemap';

// Cutover (Etapa 4 / Fase 6): o Astro É a raiz do projeto no GitHub Pages
// (`/tbh-market-tool/`); o legado foi p/ `/tbh-market-tool/legacy/`.
// site+base geram links com o prefixo correto (project pages).
// https://astro.build/config
export default defineConfig({
  site: 'https://filipefont.github.io',
  base: '/tbh-market-tool',
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