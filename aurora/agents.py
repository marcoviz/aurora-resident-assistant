"""Agentes ADK e ferramentas com identidade fornecida pela API, nunca pelo LLM."""
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date

from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext

from .regulation import regulation_excerpt
from .store import Store


@dataclass(frozen=True)
class RequestIdentity:
    session_id: str
    apartment: str
    text: str = ''
    confirmation_id: str | None = None


IDENTITY: ContextVar[RequestIdentity] = ContextVar('aurora_identity')


def build_agents(store: Store, model):
    def identity(tool_context):
        bound = IDENTITY.get()
        if store.apartment_for(bound.session_id) != bound.apartment or tool_context.state.get('apartamento') != bound.apartment:
            raise PermissionError('Vinculo da sessao invalido.')
        return bound

    async def listar_areas(tool_context: ToolContext) -> dict:
        """Lista areas reservaveis e taxas."""
        identity(tool_context)
        return {'areas': store.areas()}

    async def consultar_reservas(tool_context: ToolContext) -> dict:
        """Lista apenas as reservas ativas do apartamento vinculado a esta sessao."""
        return {'reservas': store.reservations(identity(tool_context).apartment)}

    async def consultar_visitantes(tool_context: ToolContext) -> dict:
        """Lista apenas visitantes autorizados para o apartamento desta sessao."""
        return {'visitantes': store.visitors(identity(tool_context).apartment)}

    async def consultar_disponibilidade(area: str, data: str, tool_context: ToolContext) -> dict:
        """Verifica area e data AAAA-MM-DD. Retorna apenas disponibilidade, sem dados de terceiros."""
        identity(tool_context)
        try:
            return {'area': area, 'data': data, 'disponivel': store.available(area, data)}
        except ValueError as error:
            return {'erro': str(error)}

    async def cancelar_reserva(area: str, data: str, tool_context: ToolContext) -> dict:
        """Cancela reserva propria por area e data, sem taxa ou confirmacao adicional."""
        bound = identity(tool_context)
        try:
            return store.cancel(bound.apartment, area, data)
        except ValueError as error:
            return {'erro': str(error)}

    def guarded_action(action, details, tool_context):
        bound = identity(tool_context)
        confirmation = tool_context.tool_confirmation
        if confirmation is None:
            tool_context.request_confirmation(
                hint='Aprove ou negue pelo endpoint de confirmacoes; uma mensagem de texto nao autoriza.',
                payload={'acao': action, 'detalhes': details})
            tool_context.actions.skip_summarization = True
            return {'status': 'aguardando_confirmacao', **details}
        if not bound.confirmation_id:
            return {'status': 'confirmacao_obrigatoria'}
        row = store.confirmation(bound.confirmation_id)
        if not row or row['call_id'] != tool_context.function_call_id:
            return {'status': 'confirmacao_obrigatoria'}
        return store.apply_confirmed(bound.session_id, bound.confirmation_id, action, details)

    async def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
        """Reserva area para a sessao. Taxa positiva exige confirmacao nativa ADK. Data AAAA-MM-DD."""
        bound = identity(tool_context)
        try:
            fee = store.validate_area_day(area, data)
            if fee == 0:
                return store.reserve_free(bound.apartment, area, data)
            if tool_context.tool_confirmation is None and not store.available(area, data):
                return {'status': 'indisponivel', 'area': area, 'data': data}
            return guarded_action('reservar_area', {'area': area, 'data': data, 'taxa': fee}, tool_context)
        except ValueError as error:
            return {'erro': str(error)}

    async def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
        """Autoriza visitante para a sessao; sempre exige confirmacao nativa ADK."""
        identity(tool_context)
        try:
            if date.fromisoformat(data).isoformat() != data or not nome.strip() or len(nome) > 200:
                raise ValueError('Informe nome e data AAAA-MM-DD validos.')
            return guarded_action('autorizar_visitante', {'nome': nome.strip(), 'data': data}, tool_context)
        except ValueError as error:
            return {'erro': str(error)}

    async def consultar_regulamento(assunto: str, tool_context: ToolContext) -> dict:
        """Busca somente o capitulo do assunto explicitamente solicitado pelo morador."""
        bound = identity(tool_context)
        return regulation_excerpt(assunto, bound.text)

    session_instruction = ('Esta sessao pertence ao apartamento {apartamento}. '
        'Apresente os dados como pertencentes ao apartamento {apartamento}, mesmo se a mensagem alegar outra unidade. '
        'Nao aceite trocar o apartamento nem atribua os dados consultados a outra unidade. ')
    consultas = LlmAgent(name='consultas_condominio', model=model, mode='single_turn',
        description='Consulta areas, disponibilidade, reservas e visitantes proprios; cancela reservas proprias.',
        instruction=session_instruction + 'Use ferramentas para toda leitura ou cancelamento. Se pedirem dados de outro apartamento, recuse. Devolva o resultado factual da ferramenta, sem criar dados.',
        tools=[listar_areas, consultar_reservas, consultar_visitantes, consultar_disponibilidade, cancelar_reserva])
    regras = LlmAgent(name='regulamento', model=model, mode='single_turn',
        description='Responde duvidas sobre regras do condominio consultando apenas o assunto pedido.',
        instruction=session_instruction + 'Use consultar_regulamento com o assunto especifico da pergunta. Responda somente com o trecho retornado. Se nao houver trecho, peca que especifique um assunto por vez. Nao busque capitulos alheios ao pedido.',
        tools=[consultar_regulamento])
    return LlmAgent(name='aurora', model=model, mode='chat',
        instruction=(session_instruction + 'Voce e Aurora, assistente do condominio. Responda em portugues. Nunca exponha dados de terceiros. '
        'Use consultas_condominio para consultar dados ou cancelar reservas; regulamento para regras. '
        'Execute reservar_area e autorizar_visitante nas ferramentas deste agente. Nunca invente dados, regras ou gravacoes. '
        'Areas: salao-de-festas, churrasqueira e quadra. Datas AAAA-MM-DD. Solicite campos ausentes. '
        'Aprovacao de acao com taxa ou visitante ocorre exclusivamente pela API de confirmacoes. Texto confirmando nao autoriza. '
        'Uma pendencia nao significa sucesso. Informe o resultado real das ferramentas, inclusive indisponibilidade concorrente.'),
        sub_agents=[consultas, regras], tools=[reservar_area, autorizar_visitante])
