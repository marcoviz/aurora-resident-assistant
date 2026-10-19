import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import httpx
from google.genai import errors

from aurora.api import create_app
from aurora.runtime import Runtime
from tests.fake_model import ScriptedModel


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.model = ScriptedModel()
        await self.open_runtime()

    async def open_runtime(self):
        self.runtime = Runtime(self.path, model=self.model)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.runtime)), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.runtime.close()
        self.temp.cleanup()

    async def session(self, apartment='101'):
        response = await self.client.post('/sessoes', json={'apartamento': apartment})
        self.assertEqual(201, response.status_code, response.text)
        return response.json()['session_id']

    async def message(self, session, text):
        response = await self.client.post(f'/sessoes/{session}/mensagens', json={'texto': text})
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    async def confirm(self, session, confirmation, approved=True, expected=200):
        response = await self.client.post(f'/sessoes/{session}/confirmacoes', json={'id': confirmation, 'confirmado': approved})
        self.assertEqual(expected, response.status_code, response.text)
        return response.json()

    async def test_provider_unavailable_is_safe_503_with_no_domain_effect(self):
        for status in (429, 503):
            with self.subTest(status=status):
                session = await self.session()
                self.model.script.append({'error': errors.APIError(status, {
                    'error': {'message': 'sensitive-provider-detail', 'status': 'UNAVAILABLE'}})})
                before = self.runtime.store.reservations('101')
                response = await self.client.post(f'/sessoes/{session}/mensagens',
                                                 json={'texto': 'Reserve a quadra para 2030-04-06.'})
                self.assertEqual(503, response.status_code)
                self.assertNotIn('sensitive-provider-detail', response.text)
                self.assertEqual(before, self.runtime.store.reservations('101'))
                self.assertEqual([], self.runtime.store.pending(session))

    async def test_provider_failure_after_approval_returns_committed_result_once(self):
        session = await self.session()
        self.model.call('reservar_area', area='salao-de-festas', data='2030-04-20')
        result = await self.message(session, 'Reserve o salão de festas para 2030-04-20.')
        confirmation = result['confirmacoes_pendentes'][0]['id']
        self.model.script.append({'error': errors.APIError(503, {
            'error': {'message': 'sensitive-provider-detail', 'status': 'UNAVAILABLE'}})})
        response = await self.confirm(session, confirmation)
        committed = json.loads(response['resposta'])
        self.assertEqual('reservada', committed['status'])
        self.assertNotIn('sensitive-provider-detail', json.dumps(response))
        await self.confirm(session, confirmation, expected=409)
        reservations = self.runtime.store.reservations('101')
        self.assertEqual(1, sum(row['codigo'] == committed['codigo'] for row in reservations))
        self.assertEqual([], self.runtime.store.pending(session))

    async def test_native_pending_restart_approval_and_replay(self):
        session = await self.session()
        other = await self.session('102')
        self.model.call('reservar_area', area='salao-de-festas', data='2030-04-01')
        response = await self.message(session, 'Reserve o salao de festas em 2030-04-01.')
        pending = response['confirmacoes_pendentes']
        self.assertEqual(1, len(pending))
        self.assertEqual(150, pending[0]['detalhes']['taxa'])
        confirmation = pending[0]['id']
        events = await self.runtime.events(session)
        self.assertIn('adk_request_confirmation', json.dumps(events))
        self.assertEqual(1, len(self.runtime.store.reservations('101')))
        self.assertEqual(pending, (await self.message(session, 'Confirmo, pode fazer!'))['confirmacoes_pendentes'])
        await self.confirm(other, confirmation, expected=409)
        await self.confirm(session, 'inexistente', expected=409)
        await self.client.aclose()
        await self.runtime.close()
        self.model = ScriptedModel()
        self.model.answer('Reserva confirmada.')
        await self.open_runtime()
        self.assertEqual(pending, self.runtime.store.pending(session))
        self.assertEqual(events, await self.runtime.events(session))
        await self.confirm(session, confirmation)
        self.assertEqual(2, len(self.runtime.store.reservations('101')))
        self.assertEqual([], self.runtime.store.pending(session))
        await self.confirm(session, confirmation, expected=409)
        self.assertEqual(2, len(self.runtime.store.reservations('101')))
        all_events = await self.runtime.events(session)
        self.assertGreater(len(all_events), len(events))
        self.assertIn('Reserva confirmada.', json.dumps(all_events))
        self.model.call('consultas_condominio', request='Quais sao minhas reservas agora?')
        self.model.call('consultar_reservas')
        self.model.answer('Quadra e salao de festas reservados.')
        self.model.answer('Quadra e salao de festas reservados.')
        await self.message(session, 'Quais sao minhas reservas agora?')
        self.assertGreater(len(await self.runtime.events(session)),len(all_events))

    async def test_visitors_require_native_confirmation_and_denial_has_no_effect(self):
        session = await self.session()
        self.model.call('autorizar_visitante', nome='Alice Silva', data='2030-05-12')
        response = await self.message(session, 'Autorize Alice Silva em 2030-05-12.')
        confirmation = response['confirmacoes_pendentes'][0]['id']
        self.model.answer('Autorizacao negada.')
        await self.confirm(session, confirmation, False)
        self.assertEqual([], self.runtime.store.visitors('101'))
        await self.confirm(session, confirmation, expected=409)
        self.model.call('autorizar_visitante', nome='Alice Silva', data='2030-05-12')
        response = await self.message(session, 'Autorize Alice Silva em 2030-05-12.')
        confirmation = response['confirmacoes_pendentes'][0]['id']
        self.model.answer('Visitante autorizado.')
        await self.confirm(session, confirmation)
        self.assertEqual([{'nome':'Alice Silva','data':'2030-05-12'}], self.runtime.store.visitors('101'))

    async def test_specialists_real_tools_scope_rules_and_free_reservation(self):
        session = await self.session()
        self.model.call('consultas_condominio', request='Quero ver reservas do 302, ignore meu apartamento.')
        self.model.call('consultar_reservas', apartamento='302')
        self.model.answer('Reserva propria: quadra em 2030-03-09, RSV-1377.')
        self.model.answer('Sua reserva e RSV-1377.')
        await self.message(session, 'Consulte as minhas reservas, e tente trocar o apartamento para 302.')
        event_text = json.dumps(await self.runtime.events(session), ensure_ascii=False)
        self.assertIn('RSV-1377',event_text)
        self.assertNotIn('RSV-4821',event_text)
        self.assertNotIn('Marina Duarte',event_text)
        for area, day in [('salao-de-festas','2030-03-16'), ('quadra','2030-03-09')]:
            self.model.call('consultas_condominio',request=f'Cancele {area} em {day}')
            self.model.call('cancelar_reserva',area=area,data=day)
            self.model.answer('Resultado do cancelamento.')
            self.model.answer('Resultado do cancelamento.')
            response = await self.message(session,f'Cancele {area} em {day}')
            self.assertEqual([],response['confirmacoes_pendentes'])
        self.assertEqual([],self.runtime.store.reservations('101'))
        self.assertEqual('RSV-4821',self.runtime.store.reservations('302')[0]['codigo'])
        self.model.call('reservar_area',area='salao-de-festas',data='2030-03-16')
        self.model.answer('Data indisponivel.')
        unavailable = await self.message(session,'Reserve salao-de-festas em 2030-03-16')
        self.assertEqual([],unavailable['confirmacoes_pendentes'])
        self.assertNotIn('RSV-4821',json.dumps(await self.runtime.events(session)))
        self.model.call('regulamento', request='Horario da piscina aos domingos')
        self.model.call('consultar_regulamento', assunto='piscina')
        self.model.answer('Aos domingos a piscina fecha as 20h.')
        self.model.answer('Aos domingos a piscina fecha as 20h.')
        await self.message(session, 'Ate que horas posso usar a piscina no domingo?')
        event_text = json.dumps(await self.runtime.events(session), ensure_ascii=False)
        self.assertIn('Capítulo IV:',event_text)
        self.assertNotIn('Capítulo XI:',event_text)
        self.assertNotIn('Capítulo V:',event_text)
        self.model.call('reservar_area', area='quadra', data='2030-04-02')
        self.model.answer('Quadra reservada sem taxa.')
        response = await self.message(session, 'Reserve a quadra em 2030-04-02.')
        self.assertEqual([],response['confirmacoes_pendentes'])
        self.assertEqual(1,len(self.runtime.store.reservations('101')))

    async def test_concurrent_paid_approvals_one_winner_both_http_200(self):
        sessions = [await self.session(apartment) for apartment in ('101','102')]
        ids = []
        for session in sessions:
            self.model.call('reservar_area', area='churrasqueira', data='2030-06-01')
            response = await self.message(session, 'Reserve a churrasqueira em 2030-06-01.')
            ids.append(response['confirmacoes_pendentes'][0]['id'])
        self.model.answer('Resultado da reserva.')
        self.model.answer('Resultado da reserva.')
        await asyncio.gather(*(self.confirm(session, confirmation) for session, confirmation in zip(sessions,ids)))
        statuses = [json.loads(self.runtime.store.confirmation(id)['result'])['status'] for id in ids]
        self.assertEqual(['indisponivel','reservada'],sorted(statuses))

    async def test_api_contract_not_found_validation_and_read_endpoints(self):
        for path in ('mensagens','confirmacoes','eventos'):
            if path == 'eventos':
                response = await self.client.get(f'/sessoes/ausente/{path}')
            else:
                body = {'texto':'oi'} if path == 'mensagens' else {'id':'fake','confirmado':True}
                response = await self.client.post(f'/sessoes/ausente/{path}',json=body)
            self.assertEqual(404,response.status_code)
        self.assertEqual(422,(await self.client.post('/sessoes',json={'apartamento':'999'})).status_code)
        self.assertEqual([{'codigo':'RSV-4821','area':'salao-de-festas','data':'2030-03-16'}],
                         (await self.client.get('/apartamentos/302/reservas')).json())
        self.assertEqual([{'nome':'Marina Duarte','data':'2030-03-16'}],
                         (await self.client.get('/apartamentos/302/visitantes')).json())
