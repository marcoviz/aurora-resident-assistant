from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .runtime import ModelUnavailable, Runtime
from .store import ConfirmationConflict


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SessionInput(Input):
    apartamento: str = Field(min_length=1, max_length=20)


class MessageInput(Input):
    texto: str = Field(min_length=1, max_length=16000)


class ConfirmationInput(Input):
    id: str = Field(min_length=1, max_length=200)
    confirmado: StrictBool


def create_app(runtime=None):
    @asynccontextmanager
    async def lifespan(app):
        yield
        await app.state.runtime.close()

    app = FastAPI(title='Aurora — Assistente do morador', lifespan=lifespan)
    app.state.runtime = runtime or Runtime(Path(os.getenv('AURORA_DATA_DIR') or '.data'))

    def session(session_id):
        if app.state.runtime.store.apartment_for(session_id) is None:
            raise HTTPException(404, 'Sessao nao encontrada.')
        return app.state.runtime

    async def execute(operation):
        try:
            return await operation
        except ConfirmationConflict:
            raise HTTPException(409, 'Confirmacao inexistente, de outra sessao ou ja respondida.') from None
        except ModelUnavailable as error:
            raise HTTPException(503, str(error)) from None

    @app.post('/sessoes', status_code=201)
    async def create(body: SessionInput):
        runtime = app.state.runtime
        if not runtime.store.apartment_exists(body.apartamento):
            raise HTTPException(422, 'Apartamento desconhecido.')
        return {'session_id': await runtime.create_session(body.apartamento)}

    @app.post('/sessoes/{session_id}/mensagens')
    async def message(session_id: str, body: MessageInput):
        return await execute(session(session_id).message(session_id, body.texto))

    @app.post('/sessoes/{session_id}/confirmacoes')
    async def confirm(session_id: str, body: ConfirmationInput):
        return await execute(session(session_id).confirm(session_id, body.id, body.confirmado))

    @app.get('/sessoes/{session_id}/eventos')
    async def events(session_id: str):
        return await session(session_id).events(session_id)

    @app.get('/apartamentos/{apartamento}/reservas')
    async def reservations(apartamento: str):
        return app.state.runtime.store.reservations(apartamento)

    @app.get('/apartamentos/{apartamento}/visitantes')
    async def visitors(apartamento: str):
        return app.state.runtime.store.visitors(apartamento)

    return app
