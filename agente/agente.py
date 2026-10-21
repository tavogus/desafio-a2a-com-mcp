"""Agente A2A da central de salas.

Por fora e um servidor A2A (card, SendMessage, GetTask). Por dentro e host
MCP: descobre as tools em runtime e chama o servidor em localhost:7301.
Nao ha LLM e nao ha regra de sala aqui. Conflito, politica e alternativas
sao decisao do servidor MCP. Este processo so traduz protocolo.

A ponte esta em `pausar_por_input_required` e `retomar_task`: o
input_required do MCP vira TASK_STATE_INPUT_REQUIRED, e o requestState
opaco volta no retry com um id de JSON-RPC novo.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from typing import Any

import uvicorn
from mcp import Client
from mcp.client.session import ClientSession
from mcp.shared.exceptions import MCPError
from mcp.types import ElicitResult, Implementation, InputRequiredResult
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
URL_PUBLICA = os.environ.get("AGENTE_PUBLIC_URL", f"http://127.0.0.1:{PORTA}").rstrip("/")
URI_POLITICA = "politica://uso"

ESTADO_SUBMITTED = "TASK_STATE_SUBMITTED"
ESTADO_WORKING = "TASK_STATE_WORKING"
ESTADO_INPUT = "TASK_STATE_INPUT_REQUIRED"
ESTADO_COMPLETED = "TASK_STATE_COMPLETED"
ESTADO_CANCELED = "TASK_STATE_CANCELED"
ESTADO_FAILED = "TASK_STATE_FAILED"
TERMINAIS = {ESTADO_COMPLETED, ESTADO_CANCELED, ESTADO_FAILED}

CAMPOS_RESERVA = ("sala", "inicio", "fim", "responsavel")


async def elicitation_nao_usada(_contexto: Any, _params: Any) -> ElicitResult:
    """O callback existe so para o SDK anunciar elicitation.

    O ciclo nao passa por aqui. O agente le o input_required cru em
    `session.call_tool(..., allow_input_required=True)` e devolve a
    pergunta ao cliente A2A. Responder neste callback fecharia o MRTR
    sem pausar a Task.
    """
    raise RuntimeError("a ponte nao responde elicitation dentro do cliente MCP")


def _capabilities_somente_form(original: Any) -> Any:
    """O SDK, ao ver um callback, anuncia form e url. O contrato pede so form."""

    def build(self: ClientSession, version: str) -> Any:
        caps = original(self, version)
        elicitation = getattr(caps, "elicitation", None)
        if elicitation is not None:
            elicitation.url = None
        return caps

    return build


ClientSession._build_capabilities = _capabilities_somente_form(ClientSession._build_capabilities)  # type: ignore[method-assign]


def _exigir_form_sem_url(session: Any) -> None:
    stamp = getattr(session, "_stamp", None)
    for cell in getattr(stamp, "__closure__", None) or []:
        valor = cell.cell_contents
        if isinstance(valor, dict) and "elicitation" in valor:
            if valor["elicitation"] != {"form": {}}:
                raise RuntimeError(f"capability de elicitation inesperada: {valor['elicitation']}")
            return
    raise RuntimeError("o cliente MCP nao declarou elicitation em form mode")


def propagar_traceparent(valor: str) -> str:
    """Mantem o trace-id e gera um span-id novo."""
    partes = valor.strip().split("-")
    if len(partes) == 4 and len(partes[1]) == 32 and len(partes[2]) == 16:
        return f"{partes[0]}-{partes[1]}-{secrets.token_hex(8)}-{partes[3]}"
    return valor.strip()


def meta_com_trace(traceparent: str | None) -> dict[str, str] | None:
    if not traceparent:
        return None
    return {"traceparent": propagar_traceparent(traceparent)}


def texto_da_mensagem(mensagem: dict[str, Any]) -> str:
    partes = mensagem.get("parts") or []
    return " ".join(parte.get("text", "") for parte in partes if isinstance(parte, dict)).strip()


def parse_pedido(texto: str) -> dict[str, str] | None:
    tokens = texto.split()
    if not tokens or tokens[0] != "reservar":
        return None
    campos: dict[str, str] = {}
    for token in tokens[1:]:
        if "=" not in token:
            return None
        chave, valor = token.split("=", 1)
        campos[chave] = valor
    if any(campo not in campos or not campos[campo] for campo in CAMPOS_RESERVA):
        return None
    return {campo: campos[campo] for campo in CAMPOS_RESERVA}


def parse_escolha(texto: str) -> str | None:
    if not texto.startswith("escolha="):
        return None
    return texto.split("=", 1)[1].strip()


def schema_de_propriedades(schema: Any) -> dict[str, Any]:
    if schema is None:
        return {}
    if isinstance(schema, dict):
        return schema
    if hasattr(schema, "model_dump"):
        return schema.model_dump(by_alias=True)
    return dict(schema)


def alternativas_do_input(resultado: InputRequiredResult) -> tuple[str, list[str]]:
    pedidos = resultado.input_requests or {}
    if len(pedidos) != 1:
        raise RuntimeError("o servidor MCP devolveu um input_required sem uma unica pergunta")
    chave, pedido = next(iter(pedidos.items()))
    params = pedido.params
    schema = schema_de_propriedades(getattr(params, "requested_schema", None))
    sala = (schema.get("properties") or {}).get("sala") or {}
    if sala.get("enum"):
        opcoes = [str(item) for item in sala["enum"]]
    elif "const" in sala:
        opcoes = [str(sala["const"])]
    else:
        opcoes = []
    return chave, opcoes


def linha_de_alternativas(opcoes: list[str]) -> str:
    return "alternativas: " + ", ".join(opcoes)


def achar_tool_de_reserva(tools: list[Any]) -> str:
    """Escolhe, no tools/list, a tool cujos argumentos sao o pedido de reserva."""
    nomes: list[str] = []
    for tool in tools:
        schema = schema_de_propriedades(getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None))
        propriedades = schema.get("properties") or {}
        if all(campo in propriedades for campo in CAMPOS_RESERVA):
            nomes.append(tool.name)
    if len(nomes) != 1:
        raise RuntimeError(f"esperava uma tool de reserva no tools/list, achei {nomes}")
    return nomes[0]


def versao_no_recurso(resultado: Any) -> str:
    conteudos = getattr(resultado, "contents", None) or []
    if not conteudos:
        raise RuntimeError("resources/read de politica://uso veio sem conteudo")
    texto = getattr(conteudos[0], "text", None) or ""
    primeira = texto.splitlines()[0] if texto else ""
    if ":" not in primeira:
        raise RuntimeError("a primeira linha do resource nao declara a versao")
    return primeira.split(":", 1)[1].strip()


def texto_do_resultado(resultado: Any) -> str:
    blocos = []
    for parte in getattr(resultado, "content", None) or []:
        texto = getattr(parte, "text", None)
        if texto:
            blocos.append(texto)
    return " ".join(blocos).strip()


def nova_mensagem(role: str, texto: str, task_id: str | None = None, context_id: str | None = None) -> dict[str, Any]:
    mensagem: dict[str, Any] = {
        "messageId": f"msg-{secrets.token_hex(6)}",
        "role": role,
        "parts": [{"text": texto}],
    }
    if task_id:
        mensagem["taskId"] = task_id
    if context_id:
        mensagem["contextId"] = context_id
    return mensagem


def copiar_mensagem_usuario(mensagem: dict[str, Any], task_id: str) -> dict[str, Any]:
    partes = []
    for parte in mensagem.get("parts") or []:
        if isinstance(parte, dict) and "text" in parte:
            partes.append({"text": parte["text"]})
    copia: dict[str, Any] = {
        "messageId": mensagem.get("messageId") or f"msg-{secrets.token_hex(6)}",
        "role": mensagem.get("role") or "ROLE_USER",
        "parts": partes,
    }
    if mensagem.get("taskId"):
        copia["taskId"] = task_id
    return copia


class Agente:
    def __init__(self) -> None:
        self._client: Client | None = None
        self._tool = ""
        self._politica = ""
        self._tasks: dict[str, dict[str, Any]] = {}
        self._mcp_lock = asyncio.Lock()

    async def abrir(self) -> None:
        ultimo: Exception | None = None
        for _ in range(40):
            cliente = Client(
                MCP_URL,
                mode="2026-07-28",
                cache=None,
                client_info=Implementation(name="agente-central-de-salas", version="1.0.0"),
                elicitation_callback=elicitation_nao_usada,
            )
            try:
                await cliente.__aenter__()
                _exigir_form_sem_url(cliente.session)
                listagem = await cliente.session.list_tools()
                self._tool = achar_tool_de_reserva(list(listagem.tools))
                recurso = await cliente.session.read_resource(URI_POLITICA)
                self._politica = versao_no_recurso(recurso)
                self._client = cliente
                return
            except Exception as exc:
                ultimo = exc
                await cliente.__aexit__(type(exc), exc, exc.__traceback__)
                await asyncio.sleep(0.5)
        raise SystemExit(f"nao conectei ao servidor MCP em {MCP_URL}: {ultimo}")

    async def fechar(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    async def chamar(self, **kwargs: Any) -> Any:
        if self._client is None:
            raise RuntimeError("cliente MCP ainda nao conectado")
        async with self._mcp_lock:
            return await self._client.session.call_tool(self._tool, allow_input_required=True, **kwargs)

    def card(self) -> dict[str, Any]:
        return {
            "name": "Central de Salas",
            "description": "Reserva salas de reuniao da Hill Valley Tech.",
            "provider": {
                "organization": "Hill Valley Tech",
                "url": "https://hillvalley.example",
            },
            "version": "1.0.0",
            "supportedInterfaces": [
                {
                    "url": f"{URL_PUBLICA}/a2a",
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                }
            ],
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "extendedAgentCard": False,
            },
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [
                {
                    "id": "reservar-sala",
                    "name": "Reservar sala",
                    "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                    "tags": ["salas", "agenda"],
                    "inputModes": ["text/plain"],
                    "outputModes": ["text/plain"],
                    "examples": [
                        "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                    ],
                }
            ],
        }

    def publicar(self, task: dict[str, Any]) -> dict[str, Any]:
        """A resposta A2A nunca carrega requestState, chave de input nem argumentos selados."""
        status: dict[str, Any] = {"state": task["state"]}
        if task.get("status_message") is not None:
            status["message"] = task["status_message"]
        return {
            "id": task["id"],
            "contextId": task["context_id"],
            "status": status,
            "history": task["history"],
            "artifacts": task["artifacts"],
        }

    def dizer(self, task: dict[str, Any], state: str, texto: str) -> None:
        task["state"] = state
        mensagem = nova_mensagem("ROLE_AGENT", texto, task["id"], task["context_id"])
        task["history"].append(mensagem)
        task["status_message"] = mensagem

    def nova_task(self, traceparent: str | None) -> dict[str, Any]:
        task = {
            "id": f"task-{secrets.token_hex(6)}",
            "context_id": f"ctx-{secrets.token_hex(6)}",
            "state": ESTADO_SUBMITTED,
            "history": [],
            "artifacts": [],
            "status_message": None,
            "request_state": None,
            "input_key": None,
            "alternativas": [],
            "linha": None,
            "arguments": None,
            "traceparent": traceparent,
        }
        self._tasks[task["id"]] = task
        return task

    def trace_da(self, task: dict[str, Any], header: str | None) -> str | None:
        if header:
            task["traceparent"] = header
        return task.get("traceparent")

    async def reservar(self, task: dict[str, Any], argumentos: dict[str, str], traceparent: str | None) -> None:
        task["arguments"] = dict(argumentos)
        task["state"] = ESTADO_WORKING
        try:
            resultado = await self.chamar(arguments=argumentos, meta=meta_com_trace(traceparent))
        except MCPError as exc:
            self.dizer(task, ESTADO_FAILED, exc.message)
            return
        self.aplicar_resultado(task, resultado)

    def pausar_por_input_required(self, task: dict[str, Any], resultado: InputRequiredResult) -> None:
        """A ponte, no sentido MCP -> A2A.

        O servidor terminou o tools/call pedindo informacao. A Task para em
        TASK_STATE_INPUT_REQUIRED e o requestState fica guardado nela, opaco,
        fora de qualquer mensagem devolvida ao cliente A2A.
        """
        chave, opcoes = alternativas_do_input(resultado)
        linha = linha_de_alternativas(opcoes)
        task["request_state"] = resultado.request_state
        task["input_key"] = chave
        task["alternativas"] = opcoes
        task["linha"] = linha
        self.dizer(task, ESTADO_INPUT, linha)

    def aplicar_resultado(self, task: dict[str, Any], resultado: Any) -> None:
        if isinstance(resultado, InputRequiredResult):
            self.pausar_por_input_required(task, resultado)
            return
        if getattr(resultado, "is_error", False):
            self.dizer(task, ESTADO_FAILED, texto_do_resultado(resultado) or "falha na tool")
            return
        dados = getattr(resultado, "structured_content", None) or {}
        if dados.get("reservado") is False:
            self.dizer(task, ESTADO_CANCELED, dados.get("motivo") or "recusado")
            return
        self.concluir_reserva(task, dados)

    def concluir_reserva(self, task: dict[str, Any], dados: dict[str, Any]) -> None:
        artifact = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": self._politica,
        }
        task["artifacts"] = [
            {
                "artifactId": f"art-{secrets.token_hex(6)}",
                "name": "reserva",
                "parts": [{"text": json.dumps(artifact, ensure_ascii=False)}],
            }
        ]
        self.dizer(task, ESTADO_COMPLETED, f"Reserva {artifact['reserva']} confirmada na {artifact['sala']}.")
        task["request_state"] = None

    async def retomar_task(self, task: dict[str, Any], escolha: str, traceparent: str | None) -> None:
        """A ponte, no sentido A2A -> MCP.

        escolha=recusar vira action decline. Uma sala do enum vira action
        accept com a mesma chave recebida em inputRequests. O requestState
        e ecoado sem ser aberto, e call_tool gera um id de JSON-RPC novo.
        """
        if escolha != "recusar" and escolha not in (task.get("alternativas") or []):
            self.dizer(task, ESTADO_INPUT, task["linha"])
            return

        if escolha == "recusar":
            resposta = ElicitResult(action="decline")
        else:
            resposta = ElicitResult(action="accept", content={"sala": escolha})

        task["state"] = ESTADO_WORKING
        try:
            resultado = await self.chamar(
                arguments=task["arguments"],
                input_responses={task["input_key"]: resposta},
                request_state=task["request_state"],
                meta=meta_com_trace(traceparent),
            )
        except MCPError as exc:
            self.dizer(task, ESTADO_FAILED, exc.message)
            return
        if escolha == "recusar" and not getattr(resultado, "is_error", False):
            dados = getattr(resultado, "structured_content", None) or {}
            self.dizer(task, ESTADO_CANCELED, dados.get("motivo") or "recusado")
            task["request_state"] = None
            return
        self.aplicar_resultado(task, resultado)

    async def send_message(self, mensagem: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
        texto = texto_da_mensagem(mensagem)
        task_id = mensagem.get("taskId")
        if task_id:
            task = self._tasks.get(task_id)
            if task is None:
                raise JsonRpcError(-32602, f"Task desconhecida: {task_id}")
            if task["state"] in TERMINAIS:
                raise JsonRpcError(-32602, f"Task em estado terminal: {task['state']}")
            task["history"].append(copiar_mensagem_usuario(mensagem, task["id"]))
            trace = self.trace_da(task, traceparent)
            if task["state"] != ESTADO_INPUT:
                raise JsonRpcError(-32602, f"Task nao esta aguardando escolha: {task['state']}")
            escolha = parse_escolha(texto)
            if escolha is None:
                self.dizer(task, ESTADO_INPUT, task["linha"])
                return self.publicar(task)
            await self.retomar_task(task, escolha, trace)
            return self.publicar(task)

        task = self.nova_task(traceparent)
        task["history"].append(copiar_mensagem_usuario(mensagem, task["id"]))
        # SUBMITTED fica visivel para um GetTask concorrente antes de WORKING.
        await asyncio.sleep(0)
        task["state"] = ESTADO_WORKING
        await asyncio.sleep(0)
        pedido = parse_pedido(texto)
        if pedido is None:
            self.dizer(task, ESTADO_FAILED, "pedido invalido")
            return self.publicar(task)
        await self.reservar(task, pedido, self.trace_da(task, traceparent))
        return self.publicar(task)

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self._tasks.get(task_id)
        if task is None:
            raise JsonRpcError(-32602, f"Task desconhecida: {task_id}")
        return self.publicar(task)


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


AGENTE = Agente()


async def agent_card(_request: Request) -> JSONResponse:
    return JSONResponse(AGENTE.card())


async def a2a(request: Request) -> JSONResponse:
    try:
        corpo = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
    if not isinstance(corpo, dict):
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        )
    identificador = corpo.get("id")
    metodo = corpo.get("method")
    params = corpo.get("params") or {}
    if not isinstance(params, dict):
        return JSONResponse(
            {"jsonrpc": "2.0", "id": identificador, "error": {"code": -32602, "message": "Invalid params"}}
        )
    traceparent = request.headers.get("traceparent")
    try:
        if metodo == "SendMessage":
            mensagem = params.get("message")
            if not isinstance(mensagem, dict):
                raise JsonRpcError(-32602, "SendMessage exige params.message")
            task = await AGENTE.send_message(mensagem, traceparent)
            return JSONResponse({"jsonrpc": "2.0", "id": identificador, "result": {"task": task}})
        if metodo == "GetTask":
            task_id = params.get("id") or params.get("taskId")
            if not isinstance(task_id, str) or not task_id:
                raise JsonRpcError(-32602, "GetTask exige params.id")
            return JSONResponse(
                {"jsonrpc": "2.0", "id": identificador, "result": {"task": AGENTE.get_task(task_id)}}
            )
        raise JsonRpcError(-32601, f"Method not found: {metodo}")
    except JsonRpcError as exc:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": identificador, "error": {"code": exc.code, "message": exc.message}}
        )


def criar_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
            Route("/a2a", a2a, methods=["POST"]),
        ]
    )


async def main() -> None:
    await AGENTE.abrir()
    config = uvicorn.Config(criar_app(), host=HOST, port=PORTA, log_level="warning")
    servidor = uvicorn.Server(config)
    try:
        await servidor.serve()
    finally:
        await AGENTE.fechar()


if __name__ == "__main__":
    asyncio.run(main())
