import { expect, test } from '@playwright/test';

test('/showcase: design system renderiza e o toggle de moeda propaga', async ({ page }) => {
  await page.goto('./showcase');

  // selos e grades (renderizados no servidor)
  await expect(page.getByText('AO VIVO')).toBeVisible();
  await expect(page.getByText('Arcana', { exact: true }).first()).toBeVisible();

  // a ilha interativa (client:only) hidrata e mostra os cards na moeda padrão (BRL)
  const card = page.locator('article').first();
  await expect(card).toContainText('gold / R$');

  // alternar p/ USD propaga para os cards (estado compartilhado)
  await page.getByRole('button', { name: /\$ USD/ }).click();
  await expect(card).toContainText('gold / $');
});
