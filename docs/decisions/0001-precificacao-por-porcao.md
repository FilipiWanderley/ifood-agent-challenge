# ADR 0001 - Precificação de delivery com base de custo explícita (porção e lote)

Status: aceita

## Contexto

`PricingPolicy.evaluate(recipe_cost)` calcula preço de equilíbrio e cenários de
margem a partir de `recipe_cost.total_cmv`. O `RecipeCostCalculator` produz o CMV
da receita inteira (`total_cmv`) e o CMV por porção (`cmv_per_portion`), mas a
ferramenta MCP `calculate_delivery_pricing` só repassava a análise baseada no
CMV total.

Com `servings > 1`, o "preço de venda" devolvido era o preço do lote completo e
não o preço de um prato no cardápio. Exemplo real na consultoria: a galinhada de
15 porções (CMV parcial R$ 35,548875) retornou R$ 47,40 no cenário de 15%, que é
o preço das 15 porções juntas. Para obter o preço por porção, o agente precisava
dividir os ingredientes por 15 e chamar a ferramenta de novo, o que abre espaço
para erro e para cálculo manual pelo LLM.

## Decisão

1. `PricingPolicy.evaluate_cost(cost)` concentra toda a matemática de preço
   (preço de equilíbrio, cenários, arredondamento para cima em centavos e ajuste
   até a margem efetiva atingir a meta). `evaluate(recipe_cost)` passa a delegar
   para `evaluate_cost(recipe_cost.total_cmv)`, com o mesmo comportamento de antes.
2. `PricingAnalysis` ganha `cost_basis`, a base de custo usada, para que todo
   resultado diga sobre qual custo foi calculado.
3. `DeliveryPricingCalculator` (domínio) monta um `DeliveryPricingReport` com:
   - `servings`, `total_cmv`, `cmv_per_portion`, `platform_fee_rate`;
   - `per_portion`: `evaluate_cost(cmv_per_portion)`, que é o preço de cardápio;
   - `batch` (opcional): `evaluate(recipe_cost)`, o lote completo;
   - `pending_cost_items` e `cmv_complete`: ingredientes usados mas ainda sem
     quantidade. Eles nunca entram no CMV. Se a lista não estiver vazia, o CMV e
     todos os preços ficam marcados como parciais. Um item pendente que já esteja
     quantificado em `requirements` é rejeitado, para não misturar custo
     pendente com custo validado.
   O relatório valida as próprias invariantes: `per_portion` usa o CMV por
   porção, `batch` usa o CMV total e `cmv_complete` corresponde a
   `pending_cost_items`.
4. A ferramenta MCP `calculate_delivery_pricing` aceita dois parâmetros
   opcionais, `include_batch_pricing` e `pending_cost_items`, e devolve o
   relatório em `delivery_pricing`, além de campos planos de conveniência
   (`servings`, `total_cmv_brl`, `cmv_per_portion_brl`,
   `break_even_price_per_portion_brl`, `cmv_complete`, `pending_cost_items`).

## Compatibilidade

- As chamadas antigas continuam válidas, porque os parâmetros novos são
  opcionais.
- Os campos `recipe_cost`, `pricing_analysis`, `platform_fee_rate`,
  `break_even_price_brl` e `currency` continuam presentes, com a mesma semântica
  de antes (base no CMV total). Ficam documentados como legados. Quando
  `include_batch_pricing=true`, `pricing_analysis` é idêntico a
  `delivery_pricing.batch`.
- `PricingPolicy.evaluate` mantém a mesma assinatura e os mesmos valores. A única
  mudança é o campo `cost_basis`, que foi adicionado.
- Com `servings = 1`, a análise por porção e a do lote são idênticas.

## Consequências

- O preço de cardápio vem de uma única chamada, sem divisão manual de
  ingredientes.
- Não há duplicação de lógica: porção e lote usam o mesmo
  `PricingPolicy.evaluate_cost`.
- Testes unitários e de integração (1, 4 e 15 porções) conferem as margens de
  15%, 25% e 35%, a minimalidade do preço em centavos e a equivalência entre o
  preço por porção de uma receita de 15 porções e a mesma receita dividida por 15.
