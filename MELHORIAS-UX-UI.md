# Melhorias sugeridas de Usabilidade e UI — TBH Market Tool

Sugestões para a página `index.html` (gerada por `build.py`). Organizadas por
impacto × esforço, para você escolher o que implementar primeiro. Nada aqui é
obrigatório — é um cardápio.

> Contexto atual: SPA de tabela única, tema escuro, ~653 itens, filtros + ordenação,
> toggle USD/BRL com taxa editável, modo servidor (preço real sob demanda).

---

## 🎯 Resumo — por onde começar

| Prioridade | Item | Esforço |
|---|---|---|
| 🔴 Alta | Persistir filtros/ordenação/moeda no `localStorage` | Baixo |
| 🔴 Alta | Estado vazio ("nenhum item no filtro") | Baixo |
| 🔴 Alta | Feedback de carregando/erro fora de `alert()` | Médio |
| 🟡 Média | Destaque visual do "melhor negócio" (top gold/moeda) | Baixo |
| 🟡 Média | Responsividade / uso em celular | Médio |
| 🟡 Média | Indicador de quão "velho" é o preço (timestamp por item) | Médio |
| 🟢 Baixa | Atalhos de teclado (`/` para buscar, etc.) | Baixo |
| 🟢 Baixa | Exportar CSV do filtro atual | Baixo |

---

## 1. Usabilidade (maior impacto no dia a dia)

### 1.1 Persistir preferências
Hoje, ao recarregar a página, perde-se moeda, taxa, ordenação e filtros.
- Salvar em `localStorage`: `cur`, `rate`, `sortK`, `sortDir`, `realMode`, `minlist`
  e o texto da busca.
- Restaurar no carregamento. É a melhoria com melhor relação valor/esforço.

### 1.2 Estado vazio explícito
Quando o filtro não retorna nada, a tabela some sem explicação.
- Mostrar uma linha/painel: "Nenhum item corresponde aos filtros" + botão
  **"Limpar filtros"**.

### 1.3 Botão "Limpar filtros"
Um único botão que zera busca, grade, tipo e listagens mínimas. Atalho mental
para "voltar ao estado completo".

### 1.4 Trocar `alert()` por toasts/banner inline
`alert()` bloqueia a página e parece amador. Erros de "falha ao buscar preço" e
o aviso de modo estático ficam melhores como **toast** no canto ou banner
discreto no topo, com botão de fechar.

### 1.5 Contador de resultados sempre visível
O baseline já mostra "N itens no filtro", mas misturado com outras infos.
Considerar um chip dedicado (ex.: `247 / 653`) perto da busca, deixando claro
que há um filtro ativo.

### 1.6 Loading skeleton no boot
Enquanto `detectServer()` roda e antes do primeiro `render()`, mostrar
linhas-fantasma (skeleton) em vez de tabela vazia. Sensação de velocidade.

### 1.7 Debounce na busca
`$("q").addEventListener("input", render)` re-renderiza 653 linhas a cada tecla.
Em máquinas fracas trava. Aplicar `debounce` de ~150 ms.

---

## 2. UI / Visual

### 2.1 Destacar o "melhor negócio"
A coluna **Gold / moeda** é o coração da ferramenta. Sugestões:
- **Barra de proporção** (mini data-bar) dentro da célula, relativa ao máximo
  visível — leitura instantânea de quem rende mais.
- Ou escala de cor (verde forte = melhor) nas top 3–5 linhas.

### 2.2 Cores por grade
Itens de jogo costumam ter cores por raridade. O badge de grade poderia herdar
a cor canônica (Common/Rare/Legendary…) em vez de cinza uniforme. Ajuda a
escanear visualmente.

### 2.3 Diferenciar "estimado" de "real"
Hoje as colunas est. e real ficam lado a lado com estilos parecidos. Sugestões:
- Coluna "real" com fundo levemente distinto, ou ícone ✓ quando há preço real.
- Tooltip explicando a diferença (já está no rodapé, mas longe da coluna).

### 2.4 Sinalizar liquidez de forma mais clara
A classe `.low` (vermelho) em Vol/Listagens é boa. Acrescentar um ícone ⚠️ ou
badge "baixa liquidez" deixa o risco mais óbvio para quem não conhece o código
de cores.

### 2.5 Zebra striping opcional
Linhas alternadas (`tr:nth-child(even)`) facilitam seguir a linha numa tabela
larga. O hover já existe; o zebra ajuda na leitura estática.

### 2.6 Coluna "Item" fixa ao rolar horizontalmente
Em telas estreitas, ao rolar para ver Vol/Listagens, perde-se o nome. `position:
sticky; left:0` na primeira coluna resolve.

### 2.7 Hierarquia do header
O `<h1>` e a meta estão ok, mas os controles (`.controls`) têm muitos elementos
na mesma linha. Agrupar visualmente: **[busca]** · **[filtros]** · **[moeda/taxa]**
· **[ações]**, com pequenos separadores.

---

## 3. Responsividade / Mobile

### 3.1 A tabela larga não cabe no celular
9 colunas com `white-space:nowrap` viram scroll horizontal infinito.
- Em telas estreitas, **esconder colunas secundárias** (Vol, Listagens, talvez
  preço estimado) e revelar via toque/expand.
- Ou layout em "cards" abaixo de ~600px: cada item vira um cartão com nome,
  gold/moeda em destaque e o resto em linhas menores.

### 3.2 Controles que quebram bem
`.controls` já usa `flex-wrap`, mas os campos de número (`width:64px/78px`) e a
barra de moeda ficam apertados. Revisar em 360px de largura.

### 3.3 Alvos de toque
Botões `↻` por linha (`.px`) são pequenos para dedo. Mínimo recomendado ~40px.

---

## 4. Acessibilidade (a11y)

### 4.1 Contraste e foco
- Verificar contraste de `.muted` (#5b6378) sobre `#13151a` — provavelmente abaixo
  do AA (4.5:1).
- Adicionar `:focus-visible` claro nos botões/inputs (hoje só há `:hover`).

### 4.2 Semântica de ordenação
Os `<th>` clicáveis deveriam expor `aria-sort` (`ascending`/`descending`) e ser
focáveis/acionáveis por teclado (`role`/`tabindex` ou um `<button>` interno).

### 4.3 Texto alternativo dos status
O `.dot` colorido (verde/cinza) comunica estado só por cor. O texto ao lado já
ajuda; garantir que leitores de tela leiam "servidor conectado/desconectado".

### 4.4 Idioma e títulos
`lang="pt-br"` ok. Garantir `title`/`aria-label` nos botões com só ícone (🔄, ↻, 📐).

---

## 5. Confiança nos dados (específico desta ferramenta)

### 5.1 Idade do preço por item
Preços reais ficam em `enriched.json` e "sobrevivem a rebuilds". O usuário não
sabe se o preço real é de hoje ou de uma semana atrás.
- Guardar `fetchedAt` por item e mostrar "atualizado há 2h" em tooltip/coluna.
- Destacar preços velhos (> X dias) com cor/ícone.

### 5.2 Timestamp global mais visível
"Gerado em 2026-06-02 20:37" está discreto. Para preços, frescor importa muito —
vale realçar e, no modo servidor, mostrar "bulk atualizado há N".

### 5.3 Explicar "est." vs "real" no lugar certo
Mover (ou duplicar) a nota do rodapé para um tooltip `ⓘ` no cabeçalho das
colunas. Reduz confusão sobre qual número confiar.

### 5.4 Botão "atualizar visíveis"
No modo servidor, além do `↻` por linha, um botão "buscar preço real dos N
visíveis" (respeitando o throttle) economiza cliques no top do ranking.

---

## 6. Recursos extras (nice-to-have)

- **Exportar CSV/JSON** do filtro atual — útil para planilhas.
- **Atalhos de teclado**: `/` foca a busca, `Esc` limpa, setas navegam linhas.
- **Favoritar itens** (estrela) com lista "meus itens" persistida.
- **Link compartilhável**: serializar filtros na URL (`?q=...&grade=...`) para
  mandar uma visão pronta para alguém.
- **Modo claro/escuro** (hoje é fixo dark via `color-scheme`).
- **Coluna calculada de margem**: se houver custo de aquisição, mostrar lucro
  líquido, não só gold/moeda.

---

## 7. Notas técnicas / código

- **Render incremental**: hoje `tbody.innerHTML = ...` reescreve tudo a cada
  interação. Para 653 linhas ainda é aceitável, mas combinar com o debounce
  (1.7) e/ou virtualização se a base crescer.
- **Acessar `enriched`/`steam_market` direto**: a página embute o JSON inline
  (linha gigante no HTML). Funciona offline, mas dificulta diffs no git e
  inspeção. Avaliar `fetch` do `data/*.json` quando em modo servidor (estático
  continua embutido).
- **Sanitização**: `esc()` já cobre XSS no render — manter ao adicionar colunas
  novas que venham de dados externos.

---

*Documento gerado em 2026-06-02. Sugestões priorizadas por impacto no uso real
da ferramenta (ranquear retorno em gold). Comece pelos 🔴 da tabela-resumo.*
