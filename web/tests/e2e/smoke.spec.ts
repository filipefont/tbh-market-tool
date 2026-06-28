import { expect, test } from '@playwright/test';

test('home /next carrega o Mercado e a tabela hidrata', async ({ page }) => {
  await page.goto('./');

  await expect(page.getByRole('heading', { name: 'Mercado' })).toBeVisible();

  // a ilha (client:only) hidratou: toggle de moeda e cabeçalho ordenável existem
  await expect(page.getByRole('button', { name: /R\$ BRL/ })).toBeVisible();
  const sortHeader = page.getByRole('button', { name: /Gold\/moeda/ });
  await expect(sortHeader).toBeVisible();

  // ordenar por uma coluna não quebra (marca a seta na ativa)
  await sortHeader.click();
  await expect(sortHeader).toContainText(/[▲▼]/);
});
