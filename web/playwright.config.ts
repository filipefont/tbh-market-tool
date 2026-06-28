import { defineConfig, devices } from '@playwright/test';

// E2E mínimo da Fase 0: builda + serve o site estático e valida no Chromium.
// Roda no CI como rede de segurança da migração. base do site = /tbh-market-tool.
const PORT = 4321;
// barra final é importante: com `goto('./')`, a URL resolve p/ o próprio base.
const BASE = `http://localhost:${PORT}/tbh-market-tool/`;

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: BASE,
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'npm run build && npm run preview',
    url: BASE,
    reuseExistingServer: !process.env.CI,
    // build+preview do zero; folgado p/ o I/O lento do /mnt/c+OneDrive local
    // (build ~75s aqui por causa do disco; no CI/ext4 é ~10s).
    timeout: 240_000,
  },
});
