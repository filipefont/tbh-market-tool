import { defineConfig } from 'vitest/config';

// Vitest cobre só os testes unitários; os E2E (tests/e2e) são do Playwright.
export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    exclude: ['tests/e2e/**', 'node_modules/**', 'dist/**'],
  },
});
