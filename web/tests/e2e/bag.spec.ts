import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';

const SAMPLE = fileURLToPath(new URL('../fixtures/sample.es3', import.meta.url));

test('/bag: lê o save e valora o inventário (read-only, local)', async ({ page }) => {
  await page.goto('./bag');
  await expect(page.getByRole('heading', { name: 'Minha Mochila' })).toBeVisible();

  // upload do save de exemplo (input escondido)
  await page.locator('input[type="file"]').setInputFiles(SAMPLE);

  // resultado: ouro + valor da mochila
  await expect(page.getByText('Valor da mochila')).toBeVisible();
  await expect(page.getByText('13.4M')).toBeVisible(); // ouro 13.371.337 abreviado
  // tabela com pelo menos uma linha valorada
  await expect(page.locator('tbody tr').first()).toBeVisible();
});
