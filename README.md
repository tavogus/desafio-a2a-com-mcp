# A Ponte: um agente A2A com MCP por dentro

Servidor MCP das salas da Hill Valley Tech e agente A2A que o consome por HTTP. O agente não tem LLM e não decide conflito, política nem alternativa: ele traduz protocolo. O estado que atravessa a fronteira é nomeado dos dois lados, o `requestState` do MRTR e a Task do A2A.

## Como rodar

Python 3.10 ou superior, a partir de um clone limpo, na raiz do repositório. As versões estão travadas em `pyproject.toml` e em `requirements.txt`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export REQUEST_STATE_SECRET="$(python3 -c "import secrets; print(secrets.token_hex(32))")"
```

Gere o segredo uma vez e exporte o mesmo valor no terminal do servidor MCP. Não commite o valor: o repositório é público. Se reiniciar o servidor no meio de um retry, use o mesmo segredo, senão o selo deixa de verificar.

Terminal do servidor MCP (porta 7301, stderr visível):

```bash
source .venv/bin/activate
export REQUEST_STATE_SECRET="<o valor gerado acima>"
python servidor-mcp/servidor.py
```

Terminal do agente, depois que o servidor subiu (porta 7300). O agente tenta o MCP por cerca de 20 segundos antes de desistir:

```bash
source .venv/bin/activate
python agente/agente.py
```

Validador, com os dois processos recém-iniciados. Rodar de novo sem reiniciar os dois muda as reservas em memória e produz falso negativo:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Os padrões são o agente em `7300` (`/a2a` e `/.well-known/agent-card.json`) e o MCP em `7301` (`/mcp`). Dá para trocar com `AGENTE_PORT`, `AGENTE_HOST`, `AGENTE_PUBLIC_URL`, `MCP_PORT`, `MCP_HOST` e `MCP_URL`.

## Onde a ponte acontece

Quando `reservar_sala` encontra conflito e há alternativa, o servidor não pergunta pelo canal de volta: em `servidor-mcp/servidor.py`, `reservar_sala` devolve `InputRequiredResult`, com a elicitation em form mode e o pedido original (sala, intervalo, responsável e alternativas) no `request_state` que `selar_pedido` monta. O SDK sela esse texto antes de ele sair no fio.

No agente, `Agente.pausar_por_input_required` em `agente/agente.py` é o ponto em que esse `input_required` vira `TASK_STATE_INPUT_REQUIRED`. A linha `alternativas: ...` sai na ordem do `enum` (ou do `const`, se houver uma só sala). O token fica no dict daquela Task e não entra no card, no artifact nem em mensagem A2A.

A volta é `Agente.retomar_task`. `escolha=<id>` vira `action: accept` com a mesma chave de `inputRequests`; `escolha=recusar` vira `action: decline`. O método chama `session.call_tool` de novo, com outro id de JSON-RPC, ecoando o `requestState` sem abri-lo. No servidor, `concluir_retry` lê o plaintext que o SDK já verificou e conclui a reserva com os valores selados.

## Decisões técnicas

O `requestState` usa o `RequestStateSecurity` do SDK `mcp` 2.3.0, codec AES-256-GCM. A chave é só a variável `REQUEST_STATE_SECRET` (no mínimo 32 bytes). O TTL é 15 minutos, dentro da janela de 5 a 30. O envelope também amarra método, nome da tool e o digest dos argumentos: um retry com argumentos diferentes do pedido original recebe `-32602` (`Invalid or expired requestState`) antes do handler. Quando o selo passa, `concluir_retry` ignora os argumentos reenviados e usa o JSON selado. Um token adulterado ou expirado cai no mesmo `-32602`. Como a chave vive no ambiente e o pedido vive no token, o retry funciona depois de reiniciar o processo do MCP.

As reservas ficam numa lista em memória, carregada de `dados/reservas.json` na subida. Não sobrevivem a restart. As Tasks ficam num dict em memória do processo do agente, uma entrada por Task, cada uma com o próprio `requestState`. Dois conflitos pausados ao mesmo tempo não trocam de token.

O trace-id do header `traceparent` do cliente A2A é copiado para o `_meta.traceparent` de cada request MCP daquela Task, com span-id novo. O servidor escreve no stderr, em toda requisição, `method`, `id` e `traceparent`.

Não há SDK de LLM no caminho de execução nem nas dependências. O mesmo texto de pedido produz o mesmo efeito.

## Saída do validador

Última execução, com os dois processos recém-iniciados. Código de saída 0.

```
trace-id desta execucao: 5f34d0a1c162449c12cc2eda6ba4d053
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
