"""Servidor MCP da central de salas.

Streamable HTTP stateless na porta 7301. As reservas vivem em memoria.
O requestState do MRTR nao vive aqui: o SDK sela o pedido original com
AES-256-GCM sob REQUEST_STATE_SECRET, e o retry reconstrui a reserva
a partir desse selo, mesmo depois de um restart.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver import Context, RequestStateSecurity
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import (
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ClientCapabilities,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitationCapability,
    FormElicitationCapability,
    InputRequiredResult,
    MissingRequiredClientCapabilityErrorData,
)
from pydantic import BaseModel

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"
# 15 minutos: dentro da janela exigida de 5 a 30 minutos.
TTL_REQUEST_STATE = 15 * 60
FUSO_SAO_PAULO = timezone(timedelta(hours=-3))
CHAVE_ELICITACAO = "escolha_de_sala"
MENSAGEM_ELICITACAO = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."

MSG_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
MSG_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
MSG_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
MSG_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"

HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORTA = int(os.environ.get("MCP_PORT", "7301"))


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


def _ler_json(nome: str) -> Any:
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


SALAS: list[dict[str, Any]] = _ler_json("salas.json")
SALAS_POR_ID: dict[str, dict[str, Any]] = {sala["id"]: sala for sala in SALAS}
RESERVAS: list[dict[str, str]] = _ler_json("reservas.json")


def versao_da_politica() -> str:
    primeira = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8").splitlines()[0]
    return primeira.split(":", 1)[1].strip()


VERSAO_POLITICA = versao_da_politica()


def ler_politica() -> str:
    """Conteudo integral de dados/politica-de-uso.md."""
    return (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")


def ler_segredo() -> str:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if len(segredo.encode("utf-8")) < 32:
        sys.stderr.write(
            "Defina REQUEST_STATE_SECRET com pelo menos 32 bytes de aleatoriedade.\n"
            'Gere com: python3 -c "import secrets; print(secrets.token_hex(32))"\n'
            "Exporte com: export REQUEST_STATE_SECRET=<valor>\n"
        )
        raise SystemExit(1)
    return segredo


def parse_instante(valor: str) -> datetime:
    texto = valor.strip()
    if texto.endswith("Z"):
        texto = texto[:-1] + "+00:00"
    try:
        instante = datetime.fromisoformat(texto)
    except ValueError as exc:
        raise ToolError(MSG_INTERVALO) from exc
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=FUSO_SAO_PAULO)
    return instante.astimezone(FUSO_SAO_PAULO)


def validar_intervalo(inicio: str, fim: str) -> tuple[datetime, datetime]:
    comeco = parse_instante(inicio)
    termino = parse_instante(fim)
    if termino <= comeco:
        raise ToolError(MSG_INTERVALO)
    abertura = comeco.replace(hour=8, minute=0, second=0, microsecond=0)
    fechamento = comeco.replace(hour=20, minute=0, second=0, microsecond=0)
    if comeco < abertura or termino > fechamento:
        raise ToolError(MSG_JANELA)
    if termino - comeco > timedelta(hours=2):
        raise ToolError(MSG_DURACAO)
    return comeco, termino


def exigir_sala(sala: str) -> dict[str, Any]:
    encontrada = SALAS_POR_ID.get(sala)
    if encontrada is None:
        raise ToolError(f"Sala inexistente: {sala}")
    return encontrada


def conflitos_de(sala: str, inicio: str, fim: str) -> list[dict[str, str]]:
    comeco, termino = validar_intervalo(inicio, fim)
    achados: list[dict[str, str]] = []
    for reserva in RESERVAS:
        if reserva["sala"] != sala:
            continue
        outra_inicio = parse_instante(reserva["inicio"])
        outra_fim = parse_instante(reserva["fim"])
        if comeco < outra_fim and outra_inicio < termino:
            achados.append(reserva)
    return achados


def alternativas_para(sala: str, inicio: str, fim: str) -> list[str]:
    """Salas livres no intervalo, capacidade >= a pedida, no maximo tres.

    Ordenadas por capacidade crescente e, no empate, por id alfabetico.
    """
    pedida = exigir_sala(sala)
    minimo = pedida["capacidade"]
    candidatas: list[dict[str, Any]] = []
    for outra in SALAS:
        if outra["id"] == sala or outra["capacidade"] < minimo:
            continue
        if conflitos_de(outra["id"], inicio, fim):
            continue
        candidatas.append(outra)
    candidatas.sort(key=lambda item: (item["capacidade"], item["id"]))
    return [item["id"] for item in candidatas[:3]]


def proximo_id() -> str:
    maior = 0
    for reserva in RESERVAS:
        sufixo = reserva["id"].removeprefix("res-")
        if sufixo.isdigit():
            maior = max(maior, int(sufixo))
    return f"res-{maior + 1:04d}"


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    reserva = {
        "id": proximo_id(),
        "sala": sala,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
    }
    RESERVAS.append(reserva)
    return ReservaOut(
        reserva=reserva["id"],
        reservado=True,
        sala=sala,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=VERSAO_POLITICA,
    )


def cliente_declara_form(ctx: Context) -> bool:
    """Form mode explicito, ou elicitation vazia (a spec trata {} como form)."""
    capacidades = ctx.client_capabilities
    if capacidades is None or capacidades.elicitation is None:
        return False
    elicitation = capacidades.elicitation
    if elicitation.form is not None:
        return True
    return elicitation.url is None


def erro_sem_form() -> MCPError:
    exigido = ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))
    dados = MissingRequiredClientCapabilityErrorData(required_capabilities=exigido)
    return MCPError(
        code=MISSING_REQUIRED_CLIENT_CAPABILITY,
        message="Client did not declare the form elicitation capability required to choose an alternative room",
        data=dados.model_dump(by_alias=True, mode="json", exclude_none=True),
    )


def schema_da_escolha(opcoes: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "sala": {
                "type": "string",
                "enum": opcoes,
                "title": "Sala",
                "description": "Sala alternativa escolhida",
            }
        },
        "required": ["sala"],
    }


def selar_pedido(sala: str, inicio: str, fim: str, responsavel: str, opcoes: list[str]) -> str:
    """Plaintext que o SDK sela. O handler le isto de volta; o fio so ve o token."""
    return json.dumps(
        {
            "v": 1,
            "sala": sala,
            "inicio": inicio,
            "fim": fim,
            "responsavel": responsavel,
            "alternativas": opcoes,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def resposta_da_elicitation(ctx: Context) -> Any:
    respostas = ctx.input_responses or {}
    if CHAVE_ELICITACAO in respostas:
        return respostas[CHAVE_ELICITACAO]
    if not respostas:
        return None
    return next(iter(respostas.values()))


def recusa(motivo: str = "recusado") -> ReservaOut:
    return ReservaOut(reservado=False, motivo=motivo)


def concluir_retry(ctx: Context) -> ReservaOut:
    """Reconstroi o pedido a partir do requestState selado e ignora os argumentos reenviados."""
    try:
        selado = json.loads(ctx.request_state or "")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ToolError("requestState invalido") from exc
    if not isinstance(selado, dict) or selado.get("v") != 1:
        raise ToolError("requestState invalido")

    resposta = resposta_da_elicitation(ctx)
    if resposta is None:
        raise ToolError("resposta de elicitation ausente")
    acao = getattr(resposta, "action", None)
    if acao in ("decline", "cancel"):
        return recusa()
    if acao != "accept":
        return recusa()

    conteudo = getattr(resposta, "content", None) or {}
    escolhida = conteudo.get("sala")
    opcoes = selado.get("alternativas") or []
    if not isinstance(escolhida, str) or escolhida not in opcoes:
        raise ToolError(f"Sala inexistente: {escolhida}")

    inicio = selado["inicio"]
    fim = selado["fim"]
    responsavel = selado["responsavel"]
    # Os valores selados ja passaram na politica na rodada anterior. Conferir de
    # novo evita reservar um intervalo que deixou de caber nas regras.
    exigir_sala(escolhida)
    validar_intervalo(inicio, fim)
    if conflitos_de(escolhida, inicio, fim):
        raise ToolError(MSG_SEM_ALTERNATIVA)
    return criar_reserva(escolhida, inicio, fim, responsavel)


async def listar_salas() -> ListaDeSalas:
    """Lista todas as salas com capacidade e recursos."""
    return ListaDeSalas(salas=[SalaOut.model_validate(sala) for sala in SALAS])


async def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    """Diz se uma sala esta livre no intervalo, e quais reservas conflitam."""
    exigir_sala(sala)
    ocupadas = conflitos_de(sala, inicio, fim)
    return Disponibilidade(
        sala=sala,
        livre=not ocupadas,
        conflitos=[
            ConflitoOut(
                id=item["id"],
                inicio=item["inicio"],
                fim=item["fim"],
                responsavel=item["responsavel"],
            )
            for item in ocupadas
        ],
    )


async def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    ctx: Context,
) -> ReservaOut | InputRequiredResult:
    """Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar."""
    if ctx.request_state:
        return concluir_retry(ctx)

    exigir_sala(sala)
    validar_intervalo(inicio, fim)
    if not conflitos_de(sala, inicio, fim):
        return criar_reserva(sala, inicio, fim, responsavel)

    opcoes = alternativas_para(sala, inicio, fim)
    if not opcoes:
        raise ToolError(MSG_SEM_ALTERNATIVA)
    if not cliente_declara_form(ctx):
        raise erro_sem_form()

    return InputRequiredResult(
        input_requests={
            CHAVE_ELICITACAO: ElicitRequest(
                params=ElicitRequestFormParams(
                    mode="form",
                    message=MENSAGEM_ELICITACAO,
                    requested_schema=schema_da_escolha(opcoes),
                )
            )
        },
        request_state=selar_pedido(sala, inicio, fim, responsavel, opcoes),
    )


class LogRequisicoes:
    """Registra no stderr metodo, id e traceparent de cada POST, antes de qualquer rejeicao."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        corpo = b""
        buffer: list[dict] = []
        while True:
            mensagem = await receive()
            buffer.append(mensagem)
            if mensagem["type"] != "http.request":
                break
            corpo += mensagem.get("body", b"")
            if not mensagem.get("more_body", False):
                break
        registrar(corpo)

        indice = 0

        async def replay() -> dict:
            nonlocal indice
            if indice < len(buffer):
                mensagem = buffer[indice]
                indice += 1
                return mensagem
            return await receive()

        await self.app(scope, replay, send)


def registrar(corpo: bytes) -> None:
    metodo = "?"
    identificador: Any = "?"
    traceparent = ""
    try:
        mensagem = json.loads(corpo)
    except json.JSONDecodeError:
        mensagem = None
    if isinstance(mensagem, dict):
        metodo = str(mensagem.get("method", ""))
        identificador = mensagem.get("id", "")
        meta = (mensagem.get("params") or {}).get("_meta") if isinstance(mensagem.get("params"), dict) else None
        if isinstance(meta, dict) and meta.get("traceparent"):
            traceparent = str(meta["traceparent"])
    sys.stderr.write(f"method={metodo} id={identificador} traceparent={traceparent}\n")
    sys.stderr.flush()


def criar_servidor() -> MCPServer:
    mcp = MCPServer(
        "central-de-salas",
        version="1.0.0",
        request_state_security=RequestStateSecurity(keys=[ler_segredo()], ttl=TTL_REQUEST_STATE),
    )
    mcp.tool(description="Lista todas as salas com capacidade e recursos.")(listar_salas)
    mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")(
        consultar_disponibilidade
    )
    mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")(
        reservar_sala
    )
    mcp.resource(
        "politica://uso",
        mime_type="text/markdown",
        name="politica-de-uso",
        description="Politica de uso das salas de reuniao.",
    )(ler_politica)
    return mcp


def main() -> None:
    mcp = criar_servidor()
    aplicacao = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host=HOST,
    )
    uvicorn.run(LogRequisicoes(aplicacao), host=HOST, port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
