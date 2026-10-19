"""Adaptador da API para o Runner e a confirmacao nativa do ADK."""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from google.adk.apps import App, ResumabilityConfig
from google.adk.models.google_llm import Gemini
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import errors, types

from .agents import IDENTITY, RequestIdentity, build_agents
from .ownership import DirectoryLease
from .store import ConfirmationConflict, Store

APP_NAME = 'aurora_resident'
USER_ID = 'resident'


class ModelUnavailable(Exception):
    pass


class Runtime:
    def __init__(self, directory: Path, *, model=None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lease = DirectoryLease(self.directory)
        try:
            self._initialize(model)
        except BaseException:
            self.lease.close()
            raise

    def _initialize(self, model):
        self.store = Store(self.directory / 'condominio.db')
        self.store.recover_interrupted()
        self.sessions = DatabaseSessionService(db_url=f'sqlite+aiosqlite:///{self.directory / "sessions.db"}')
        self.injected_model = model is not None
        provider = model or Gemini(
            model=os.getenv('GEMINI_MODEL') or 'gemini-3.1-flash-lite',
            retry_options=types.HttpRetryOptions(
                attempts=5, initial_delay=4, max_delay=30,
                http_status_codes=[429, 500, 502, 503, 504]))
        agent = build_agents(self.store, provider)
        app = App(name=APP_NAME, root_agent=agent, resumability_config=ResumabilityConfig(is_resumable=True))
        self.runner = Runner(app=app, session_service=self.sessions)
        self.locks = {}

    def lock(self, session_id):
        return self.locks.setdefault(session_id, asyncio.Lock())

    def check_model(self):
        if not self.injected_model and not os.getenv('GOOGLE_API_KEY'):
            raise ModelUnavailable('Configure GOOGLE_API_KEY para conversar com o Gemini.')

    async def close(self):
        try:
            await self.runner.close()
            await self.sessions.close()
        finally:
            self.lease.close()

    async def create_session(self, apartment):
        session_id = str(uuid4())
        await self.sessions.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id,
                                          state={'apartamento': apartment})
        self.store.bind(session_id, apartment)
        return session_id

    async def events(self, session_id):
        session = await self.sessions.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
        return [event.model_dump(mode='json') for event in session.events]

    def capture_confirmation(self, session_id, event):
        for call in event.get_function_calls():
            if call.name != 'adk_request_confirmation':
                continue
            args = call.args
            original = args['originalFunctionCall']
            payload = args['toolConfirmation'].get('payload') or {}
            if payload.get('acao') not in ('reservar_area', 'autorizar_visitante'):
                raise ValueError('Confirmacao sem acao valida.')
            self.store.save_confirmation(confirmation_id=call.id, session_id=session_id,
                invocation_id=event.invocation_id, call_id=original['id'], action=payload['acao'],
                details=payload['detalhes'])

    async def invoke(self, session_id, content, *, text='', confirmation_id=None, invocation_id=None):
        token = IDENTITY.set(RequestIdentity(session_id, self.store.apartment_for(session_id), text, confirmation_id))
        answer = ''
        try:
            async for event in self.runner.run_async(user_id=USER_ID, session_id=session_id,
                    new_message=content, invocation_id=invocation_id):
                self.capture_confirmation(session_id, event)
                if event.is_final_response() and event.content:
                    message = ''.join(part.text or '' for part in event.content.parts or [])
                    if message:
                        answer = message
        except errors.APIError as error:
            if error.code in (429, 500, 502, 503, 504):
                raise ModelUnavailable('Gemini temporariamente indisponivel ou com limite de uso atingido. Tente novamente mais tarde.') from None
            raise
        finally:
            IDENTITY.reset(token)
        pending = self.store.pending(session_id)
        return {'resposta': answer or ('Aguardando confirmacao pela API.' if pending else 'Solicitacao processada.'),
                'confirmacoes_pendentes': pending}

    async def message(self, session_id, text):
        async with self.lock(session_id):
            if self.store.pending(session_id):
                return {'resposta': 'Existe uma acao pendente. Aprove ou negue pelo endpoint de confirmacoes.',
                        'confirmacoes_pendentes': self.store.pending(session_id)}
            self.check_model()
            return await self.invoke(session_id, types.Content(role='user', parts=[types.Part(text=text)]), text=text)

    async def confirm(self, session_id, confirmation_id, approved):
        async with self.lock(session_id):
            current = self.store.confirmation(confirmation_id)
            if not current or current['session_id'] != session_id or current['status'] != 'pending':
                raise ConfirmationConflict()
            self.check_model()
            record = self.store.claim(session_id, confirmation_id, approved)
            payload = {'acao': record['action'], 'detalhes': record['details']}
            content = types.Content(role='user', parts=[types.Part(function_response=types.FunctionResponse(
                id=confirmation_id, name='adk_request_confirmation',
                response={'confirmed': approved, 'payload': payload}))])
            try:
                response = await self.invoke(session_id, content, confirmation_id=confirmation_id,
                                             invocation_id=record['invocation_id'])
            except Exception:
                current = self.store.confirmation(confirmation_id)
                if current['result']:
                    # O efeito ja foi efetivado. Uma falha posterior do modelo nao deve induzir repeticao.
                    return {'resposta': json.dumps(json.loads(current['result']), ensure_ascii=False),
                            'confirmacoes_pendentes': self.store.pending(session_id)}
                self.store.release_unfinished(confirmation_id)
                raise
            if not approved:
                self.store.finish_denied(confirmation_id)
            if approved and self.store.confirmation(confirmation_id)['status'] != 'done':
                self.store.release_unfinished(confirmation_id)
                raise RuntimeError('ADK nao retomou a ferramenta original; acao nao efetivada.')
            response['confirmacoes_pendentes'] = self.store.pending(session_id)
            return response
