# Sabor da Maria — roteiro de demonstração (7 a 8 minutos)

> **Escopo:** demonstração de engenharia para o desafio Senior AI Engineer do iFood. As condições da cozinha e os preços do cenário de homologação são fictícios; não apresentar como operação comercial ou laudo sanitário. O vídeo consta na seção de entregáveis (5–10 min), embora o cabeçalho do desafio o descreva como opcional.

## 0:00–0:50 — Problema e arquitetura

**Tela:** README e árvore `src/sabor_da_maria_agent/`.

**Fala sugerida:** “O problema não é só sugerir receitas. Precisamos conhecer o estoque, verificar se Dona Maria consegue produzir o prato, respeitar R$ 80 de compras e calcular CMV e preço somente após ela aceitar. Separei linguagem natural, pesquisa e explicações no Hermes; as regras de produção, estoque, orçamento, aprovação e matemática ficam no domínio Python exposto por MCP.”

**Mostrar:** `domain/`, `application/`, `infrastructure/mcp/`, `infrastructure/persistence/sqlite.py`.

## 0:50–1:40 — Contexto, modelo e skills

**Tela:** `.hermes/skills/dona-maria-consultora/SKILL.md`, seção “Architecture choices” do README.

**Fala sugerida:** “Uso Hermes com modelo Claude Opus 5.5, configurado externamente. A skill faz as perguntas corretas e obriga o uso dos MCPs. O LLM não é a fonte de verdade financeira; a planilha e a máquina de estados persistida são. O contexto é separado de dados operacionais e de decisões humanas auditáveis.”

**Demonstrar:** `hermes mcp test sabor-da-maria` e as 11 ferramentas; mostrar `hermes`.

## 1:40–3:05 — Descoberta, limites e reavaliação

**Tela:** conversa Hermes (com registros históricos da simulação, se ainda disponíveis) e estado da sessão.

**Narrativa:** “Pesquisei receitas com URLs, comparei a despensa e elicitei equipamentos, habilidades e capacidade. Quando a fixture de teste não incluía fogão, habilidades, gás e água, a avaliação retornou inviável — sem preencher lacunas por conta própria. Depois dos dados explícitos, o sistema invalidou a avaliação anterior e reavaliou.”

**Mostrar:** `get_planning_status` e, se possível, a sequência `inviável -> aguardando aprovação`.

## 3:05–4:15 — Aprovação explícita e integridade

**Tela:** candidato, versão aprovada 4, versão 5 após aprovação, evento `candidate_approved`, fingerprint.

**Narrativa:** “A aprovação não é um ‘sim’ interpretado informalmente: a ferramenta persiste a decisão da pessoa, amarra à versão e calcula uma impressão digital dos inputs. Alterações relevantes invalidam aprovações anteriores. O usuário autorizou separadamente o cálculo financeiro.”

**Não:** fazer uma nova aprovação em nome da pessoa, omitir que a identidade não é autenticada.

## 4:15–5:40 — CMV, compras e cenários

**Tela:** resultado persistido do cálculo, versão 6.

**Mostrar:** 15 porções, 3 meios lotes, compras R$ 37,66, CMV de ingredientes R$ 64,05, CMV/porção R$ 4,27, taxa simulada de 10%, cenários de 15%, 25% e 35%, preço ilustrativo de R$ 6,57 na opção 25%.

**Narrativa:** “Comprar pacotes custa R$ 37,66; o CMV consome só a fração efetivamente utilizada, somada aos ingredientes existentes. O total é calculado em Decimal e só arredondado na apresentação. A taxa incide sobre o preço cheio. Os valores não incluem embalagens, mão de obra, energia ou impostos; não afirmo lucro líquido.”

## 5:40–6:35 — Testes e observabilidade

**Tela:** PowerShell e GitHub Actions do PR #1.

```powershell
uv run --locked pytest tests/unit tests/integration tests/evaluation tests/contract -q
uv run --locked ruff check src tests evals
uv run --locked mypy src
uv run --locked pyright
hermes mcp test sabor-da-maria
gh pr checks 1 --repo FilipiWanderley/ifood-agent-challenge
```

**Narrativa:** “A suíte local passou com 276 testes; o Pull Request #1 passou no GitHub Actions. Existem testes unitários, de integração, contrato MCP e de avaliação financeira.”

## 6:35–7:30 — Trade-offs e fechamento

**Fala sugerida:** “Escolhi persistência SQLite local e regras determinísticas para garantir uma trilha reprodutível. As cotações capturadas via busca são rotuladas não verificadas; a autenticação do aprovador, o despacho alimentar em produção e a persistência da escolha comercial de preço são limites declarados. Na sessão real, o sistema pediu mais dados em vez de inventar confirmação. Essa é a segurança que busquei na solução.”

**Tela:** seção Limitations do README e link para o PR.

## Checklist de gravação

- Gravar a janela inteira do PowerShell/editor com texto legível e áudio claro.
- Não mostrar token Anthropic, config de credenciais, variáveis secretas, e-mails privados ou arquivos da pasta pessoal.
- Se reproduzir dados de homologação em uma nova máquina, criar **nova sessão fictícia**: SQLite local e histórico Hermes podem não estar presentes em um clone limpo.
- Evitar apresentar dados fictícios de temperatura/segurança como medidas reais.
- Não alterar a sessão real do teste; usar novas fixtures se necessário.
- Validar a duração segundo o enunciado (5–10 minutos).
- Mostrar hash/PR apenas como evidência, sem alegar um merge que não ocorreu.
