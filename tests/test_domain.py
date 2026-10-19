from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aurora.agents import regulation_excerpt
from aurora.store import Store


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'data.db')

    def test_seed_scope_cancel_and_never_reuse_code(self):
        self.assertEqual(['RSV-1377'], [row['codigo'] for row in self.store.reservations('101')])
        self.assertEqual('nao_encontrada', self.store.cancel('101', 'salao-de-festas', '2030-03-16')['status'])
        self.assertFalse(self.store.available('salao-de-festas', '2030-03-16'))
        self.assertEqual('cancelada', self.store.cancel('101', 'quadra', '2030-03-09')['status'])
        first = self.store.reserve_free('101', 'quadra', '2030-03-09')
        self.store.cancel('101', 'quadra', '2030-03-09')
        second = self.store.reserve_free('101', 'quadra', '2030-03-09')
        self.assertNotEqual(first['codigo'], second['codigo'])
        self.assertNotEqual('RSV-1377', second['codigo'])

    def test_database_enforces_concurrent_exclusivity(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda apartment: self.store.reserve_free(apartment, 'quadra', '2031-01-10'), ['101', '102']))
        self.assertEqual(['indisponivel', 'reservada'], sorted(r['status'] for r in results))

    def test_confirmations_are_durable_scoped_and_idempotent(self):
        self.store.bind('s1', '101')
        details = {'nome': 'Alice', 'data': '2030-05-01'}
        self.store.save_confirmation(confirmation_id='c1', session_id='s1', invocation_id='i1', call_id='f1', action='autorizar_visitante', details=details)
        self.assertEqual('confirmacao_obrigatoria', self.store.apply_confirmed('s1','c1','autorizar_visitante',details)['status'])
        self.store.claim('s1', 'c1', True)
        reopened = Store(self.store.path)
        a = reopened.apply_confirmed('s1','c1','autorizar_visitante',details)
        b = reopened.apply_confirmed('s1','c1','autorizar_visitante',details)
        self.assertEqual(a,b)
        self.assertEqual(1,len(reopened.visitors('101')))
        self.assertEqual([],reopened.visitors('102'))

    def test_rule_retrieval_contains_only_requested_chapter(self):
        excerpt = regulation_excerpt('piscina', 'Ate que horas posso usar a piscina no domingo?')['trecho']
        self.assertIn('20h', excerpt)
        self.assertIn('Capítulo IV:', excerpt)
        self.assertNotIn('Capítulo V:', excerpt)
        self.assertEqual('especifique_assunto', regulation_excerpt('garagem', 'horario da piscina')['status'])
        self.assertEqual('especifique_assunto', regulation_excerpt('tudo', 'piscina')['status'])
