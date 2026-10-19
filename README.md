# Aurora — assistente do morador

API para consultar o condomínio, reservar áreas e autorizar visitantes. O Gemini conduz a conversa; as ferramentas aplicam as permissões e o SQLite decide a exclusividade das reservas.

## Arquitetura

Python 3.12, Google ADK **2.9.1**, FastAPI e SQLite, com dependências resolvidas em `uv.lock`. Há três agentes em [`aurora/agents.py`](aurora/agents.py), todos configurados para Gemini:

| Agente | Responsabilidade | Acionamento |
|---|---|---|
| `aurora` | Conversar, distribuir consultas e executar pedidos de reserva ou autorização | `Runner` a cada mensagem da API |
| `consultas_condominio` | Consultar áreas, disponibilidade, reservas e visitantes; cancelar reservas próprias | Subagente ADK `single_turn`, acionado pelo principal |
| `regulamento` | Buscar o capítulo pertinente e responder à dúvida sobre regras | Subagente ADK `single_turn`, acionado pelo principal |

Os especialistas executam ferramentas no contexto da mesma sessão persistida. As duas ferramentas que podem pedir confirmação ficam no principal: a resposta nativa retorna ao mesmo agente que a solicitou, evitando uma transferência ambígua durante a retomada. O `App` habilita `ResumabilityConfig(is_resumable=True)`.

[`aurora/runtime.py`](aurora/runtime.py) adapta mensagens HTTP ao `Runner`. O `DatabaseSessionService` do ADK guarda histórico e estado em `.data/sessions.db`; [`aurora/store.py`](aurora/store.py) guarda dados do condomínio, vínculos e confirmações em `.data/condominio.db`. O diretório é configurável. Não é preciso serviço externo de banco.

O modelo padrão é `gemini-3.1-flash-lite`, validado na API do Google AI Studio em 19/10/2026. A [documentação oficial de modelos Gemini](https://ai.google.dev/gemini-api/docs/models) descreve as opções disponíveis. `GEMINI_MODEL` permite escolher outro Gemini disponível no projeto do Google AI Studio. Disponibilidade e cota dependem desse projeto. O runtime configura até cinco tentativas da chamada ao modelo para erros transitórios 429/5xx, com espera exponencial de 4 a 30 segundos; isso não repete a execução de ferramentas. Se a indisponibilidade persistir, a API responde 503 sem expor detalhes internos do provedor. Esse erro não desfaz uma operação gratuita que a ferramenta já tenha concluído antes da falha; as rotas de verificação mostram o estado persistido. Nas aprovações, se o efeito já foi gravado, o runtime devolve seu resultado persistido mesmo quando a resposta posterior do modelo falha. A integração usa a [confirmação nativa documentada pelo ADK](https://adk.dev/tools-custom/confirmation/), por `adk_request_confirmation` e `FunctionResponse`.

## Garantias

1. **Cobrança e acesso exigem confirmação.** Em [`guarded_action`](aurora/agents.py), a ferramenta chama `ToolContext.request_confirmation` e retorna sem gravar. `Runtime.capture_confirmation` persiste o identificador emitido pelo ADK, o `invocation_id`, a chamada original e os detalhes. `Runtime.confirm` aceita somente uma pendência da sessão e envia ao Runner a `FunctionResponse` nativa com esse mesmo identificador. `Store.claim` faz a transição atômica; `Store.apply_confirmed` valida sessão, ação e detalhes e grava efeito e resultado juntos. Negar não cria reserva nem visitante. Reenvio ou identificador de outra sessão recebe **409**. A mensagem “confirmo” nunca chama esse caminho. Enquanto houver pendência, novas mensagens recebem a lista de pendências e não iniciam outra execução. Quadra sem taxa e cancelamento próprio não pedem confirmação.

2. **Apartamento fixo e isolamento em código.** `Runtime.create_session` define `apartamento` no estado ADK e grava um vínculo imutável em `bindings`. A API constrói `RequestIdentity`; `identity`, em [`agents.py`](aurora/agents.py), confere esse vínculo com o estado em toda ferramenta. Nenhuma ferramenta recebe apartamento como argumento do modelo. Consultas e cancelamentos usam `WHERE apartment=?`; a disponibilidade retorna apenas um booleano. Não há nome, código ou apartamento do proprietário de uma data ocupada no resultado dessa consulta.

3. **Persistência após reinício.** O histórico completo usa o serviço oficial SQLite do ADK; dados e pendências também ficam em SQLite. Ao aprovar, o Runner recebe o `invocation_id` original, inclusive após recriar o runtime. O resultado consumido fica na mesma transação que o efeito; uma falha posterior do LLM não causa repetição. Reservas canceladas permanecem como registros inativos; códigos novos usam UUID e a chave primária inclui todos os registros históricos.

4. **Regulamento por assunto.** `regulation_excerpt`, em [`agents.py`](aurora/agents.py), relaciona o assunto pedido pela ferramenta à mensagem original recebida pela API, separa o documento por capítulos e devolve somente os pertinentes. Uma tentativa de buscar garagem durante uma pergunta sobre piscina não retorna esse capítulo. As instruções dos agentes não incluem o documento. Perguntas sem assunto reconhecido pedem esclarecimento. Os capítulos entram no histórico somente quando consultados.

5. **Exclusividade no instante da escrita.** [`store.py`](aurora/store.py) cria `UNIQUE(area,day) WHERE active=1`. A aprovação usa `BEGIN IMMEDIATE`, e `_reserve` transforma o conflito da restrição em `status: indisponivel`. Dois apartamentos podem obter pendências, mas somente um grava a reserva. Ambas as aprovações recebem resposta HTTP **200**. A consulta de disponibilidade anterior serve apenas para orientar a conversa.

As rotas de verificação de apartamentos são administrativas por contrato do desafio e leem diretamente o banco. A identidade informada em `POST /sessoes` representa autenticação já feita; autenticação real está fora do escopo desta API.

## Como rodar

Instale Python 3.12 ou superior e [uv](https://docs.astral.sh/uv/). Na raiz do projeto:

```bash
cp .env.example .env
uv sync --locked
```

Preencha `GOOGLE_API_KEY` no `.env` com a chave do Google AI Studio. As outras variáveis são opcionais: `GEMINI_MODEL` seleciona o modelo (padrão `gemini-3.1-flash-lite`); `AURORA_DATA_DIR` seleciona o diretório dos dois bancos (padrão `.data`). O `.env.example` contém somente nomes, sem valores. Não envie o `.env` ao Git.

Com a API parada, restaure as sementes:

```bash
uv run aurora-reset
```

Esse comando restaura reservas e visitantes a partir de `dados/` e também apaga sessões e pendências. Os cinco arquivos originais em `dados/` não são modificados. Depois suba:

```bash
uv run aurora-api
```

A API responde em `http://localhost:8000`, com documentação interativa em `http://localhost:8000/docs`. Para manter conversas e dados, reinicie apenas com `aurora-api`, sem executar `aurora-reset`.

```bash
curl -s http://localhost:8000/apartamentos/101/reservas
curl -s -X POST http://localhost:8000/sessoes \
  -H 'Content-Type: application/json' -d '{"apartamento":"101"}'
```

Use o `session_id` retornado:

```bash
curl -s -X POST http://localhost:8000/sessoes/SESSION_ID/mensagens \
  -H 'Content-Type: application/json' \
  -d '{"texto":"Reserve o salão de festas para 2030-04-20."}'
curl -s -X POST http://localhost:8000/sessoes/SESSION_ID/confirmacoes \
  -H 'Content-Type: application/json' \
  -d '{"id":"ID_DA_PENDENCIA","confirmado":true}'
curl -s http://localhost:8000/sessoes/SESSION_ID/eventos
```

`confirmado:false` nega a ação. Identificador desconhecido, de outra sessão ou já respondido recebe 409. Caminhos de sessão inexistente recebem 404. Campos extras e formatos inválidos recebem 422. Sem `GOOGLE_API_KEY`, as rotas de conversa retornam 503; não há modelo de demonstração nem sucesso simulado na aplicação.

Para executar os testes sem rede:

```bash
uv run python -m unittest discover -v
```

Os testes usam [`tests/fake_model.py`](tests/fake_model.py) para substituir **somente a geração do LLM** por chamadas roteirizadas. FastAPI, ferramentas, Runner, eventos, confirmação nativa e `DatabaseSessionService` SQLite são os componentes reais. Isso testa efeitos, isolamento, retomada, negação, reenvio, consultas especializadas e concorrência sem consumir cota. Essa suíte verifica as invariantes sem depender da disponibilidade do provedor. A validação com linguagem natural e conexão real ao Gemini está registrada abaixo.

Resultado local em 19/10/2026, Python 3.12.14 e ADK 2.9.1:

```text
Ran 11 tests in 1.368s

OK
```

Dois testes adicionais verificam erros transitórios do provedor: HTTP 503 sem detalhes internos antes de qualquer efeito, e retorno do resultado persistido quando o Gemini falha depois de uma aprovação já gravada, sem duplicação no reenvio. A suíte verifica a preservação exata dos eventos ao fechar e recriar o runtime, aprova a confirmação pendente após essa recriação e envia uma nova mensagem pela mesma sessão. Também executa consultas pelos dois especialistas, cancelamento próprio e tentativa de cancelar reserva alheia, negação de visitante, reenvio 409, quadra sem confirmação e disputa simultânea com duas respostas HTTP 200 e apenas uma reserva.

Com a porta 8000 livre, o smoke abaixo sobe os comandos reais em processos separados, usa dados temporários, reinicia a API e confere contratos HTTP sem chamar o Gemini:

```bash
uv run python tests/smoke_http.py
```

Saída real do smoke em 19/10/2026:

```text
Dados iniciais restaurados; sessoes e pendencias removidas.
PASS HTTP sementes restauradas pelo CLI
PASS HTTP sessao criada e eventos consultaveis
PASS HTTP ausencia de chave retorna 503, sem resposta simulada
PASS HTTP contratos de erro 409 e 404
PASS HTTP processo reiniciado com sessao e dados persistidos
5/5 smoke HTTP PASS; Gemini nao foi chamado.
```


## Validação com Gemini real

Em **19/10/2026**, a API foi executada em processos reais com `gemini-3.1-flash-lite`, Runner ADK e os dois bancos SQLite, sem componentes simulados. Foram aprovadas **21 verificações**, cobrindo os 14 passos funcionais do fluxo do avaliador, além da preservação dos cinco arquivos originais. A execução durou aproximadamente 5 minutos e 50 segundos; o cliente de validação espaçou os turnos para respeitar a cota do projeto.

A [evidência JSON](evidence/gemini-http-validation.json) contém requisições e respostas HTTP, eventos ADK, chamadas de ferramentas, hashes das sementes e metadados de uso. Foram registradas 34 respostas do modelo com metadados de uso; tentativas transitórias sem resposta não entram nessa contagem.

- Pedidos para ler ou cancelar dados do 302, feitos pela sessão do 101, não expuseram nem alteraram esses dados.
- Cancelamento próprio e quadra gratuita funcionaram sem confirmação. Salão e visitante só produziram efeito pela rota de confirmações; negação, reenvio e confirmação de outra sessão respeitaram o contrato.
- O especialista consultou apenas o capítulo da piscina e respondeu **20h** para o fechamento aos domingos. Os eventos não continham capítulos alheios ao assunto.
- Os **65 eventos** de S1 foram preservados integralmente depois de parar e subir a API. Uma nova mensagem na mesma sessão aumentou o histórico para **74 eventos**, mantendo reservas e visitante.
- Duas aprovações simultâneas para o mesmo salão e data receberam **HTTP 200**; somente uma reserva permaneceu ativa.

Esses resultados registram uma execução real e não garantem disponibilidade ou cota futura do Gemini. A validação não envia nada para a plataforma do curso.
