"""Regras invariantes e persistencia transacional, independentes do modelo."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class ConfirmationConflict(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS apartments(number TEXT PRIMARY KEY, resident TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS areas(id TEXT PRIMARY KEY, name TEXT NOT NULL, fee REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS reservations(
                    code TEXT PRIMARY KEY, apartment TEXT NOT NULL REFERENCES apartments(number),
                    area TEXT NOT NULL REFERENCES areas(id), day TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_reservation
                    ON reservations(area,day) WHERE active=1;
                CREATE TABLE IF NOT EXISTS visitors(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, apartment TEXT NOT NULL REFERENCES apartments(number),
                    name TEXT NOT NULL, day TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bindings(
                    session_id TEXT PRIMARY KEY, apartment TEXT NOT NULL REFERENCES apartments(number));
                CREATE TABLE IF NOT EXISTS confirmations(
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES bindings(session_id),
                    invocation_id TEXT NOT NULL, call_id TEXT NOT NULL, action TEXT NOT NULL,
                    details TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', approved INTEGER,
                    result TEXT, UNIQUE(session_id,call_id));
            ''')
            if not db.execute('SELECT 1 FROM apartments LIMIT 1').fetchone():
                self._seed(db)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _seed(db):
        def rows(name): return json.loads((ROOT / 'dados' / name).read_text())
        db.executemany('INSERT OR IGNORE INTO apartments VALUES (?,?)', [(r['numero'], r['morador']) for r in rows('apartamentos.json')])
        db.executemany('INSERT OR IGNORE INTO areas VALUES (?,?,?)', [(r['id'], r['nome'], r['taxa']) for r in rows('areas.json')])
        db.executemany('INSERT INTO reservations VALUES (?,?,?,?,1)', [(r['codigo'], r['apartamento'], r['area'], r['data']) for r in rows('reservas.json')])
        db.executemany('INSERT INTO visitors(apartment,name,day) VALUES (?,?,?)', [(r['apartamento'], r['nome'], r['data']) for r in rows('visitantes.json')])

    def reset(self):
        with self.connection() as db:
            db.executescript('DELETE FROM confirmations; DELETE FROM bindings; DELETE FROM visitors; DELETE FROM reservations;')
            self._seed(db)

    def apartment_exists(self, apartment):
        with self.connection() as db:
            return db.execute('SELECT 1 FROM apartments WHERE number=?', (apartment,)).fetchone() is not None

    def bind(self, session_id, apartment):
        with self.connection() as db:
            db.execute('INSERT INTO bindings VALUES (?,?)', (session_id, apartment))

    def apartment_for(self, session_id):
        with self.connection() as db:
            row = db.execute('SELECT apartment FROM bindings WHERE session_id=?', (session_id,)).fetchone()
            return row['apartment'] if row else None

    def areas(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT id,name AS nome,fee AS taxa FROM areas ORDER BY id')]

    def validate_area_day(self, area, day):
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError('Data deve usar AAAA-MM-DD.')
        with self.connection() as db:
            row = db.execute('SELECT fee FROM areas WHERE id=?', (area,)).fetchone()
            if row is None: raise ValueError('Area desconhecida.')
            return row['fee']

    def reservations(self, apartment):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT code AS codigo,area,day AS data FROM reservations WHERE apartment=? AND active=1 ORDER BY day,code', (apartment,))]

    def visitors(self, apartment):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT name AS nome,day AS data FROM visitors WHERE apartment=? ORDER BY day,id', (apartment,))]

    def available(self, area, day):
        self.validate_area_day(area, day)
        with self.connection() as db:
            return db.execute('SELECT 1 FROM reservations WHERE area=? AND day=? AND active=1', (area, day)).fetchone() is None

    def cancel(self, apartment, area, day):
        self.validate_area_day(area, day)
        with self.connection() as db:
            result = db.execute('UPDATE reservations SET active=0 WHERE apartment=? AND area=? AND day=? AND active=1', (apartment, area, day))
            return {'status': 'cancelada' if result.rowcount else 'nao_encontrada', 'area': area, 'data': day}

    @staticmethod
    def _reserve(db, apartment, details):
        code = 'AUR-' + str(uuid4())
        try:
            db.execute('INSERT INTO reservations VALUES (?,?,?,?,1)', (code, apartment, details['area'], details['data']))
        except sqlite3.IntegrityError:
            return {'status': 'indisponivel', 'area': details['area'], 'data': details['data']}
        return {'status': 'reservada', 'codigo': code, 'area': details['area'], 'data': details['data']}

    def reserve_free(self, apartment, area, day):
        if self.validate_area_day(area, day) != 0:
            return {'status': 'confirmacao_obrigatoria'}
        with self.connection() as db:
            return self._reserve(db, apartment, {'area': area, 'data': day})

    def save_confirmation(self, *, confirmation_id, session_id, invocation_id, call_id, action, details):
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO confirmations(id,session_id,invocation_id,call_id,action,details) VALUES (?,?,?,?,?,?)',
                       (confirmation_id, session_id, invocation_id, call_id, action, json.dumps(details, sort_keys=True)))

    def pending(self, session_id):
        with self.connection() as db:
            return [{'id': row['id'], 'acao': row['action'], 'detalhes': json.loads(row['details'])}
                    for row in db.execute("SELECT * FROM confirmations WHERE session_id=? AND status='pending' ORDER BY rowid", (session_id,))]

    def claim(self, session_id, confirmation_id, approved):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM confirmations WHERE id=? AND session_id=? AND status='pending'", (confirmation_id, session_id)).fetchone()
            if row is None: raise ConfirmationConflict()
            db.execute("UPDATE confirmations SET status='responding',approved=? WHERE id=?", (int(approved), confirmation_id))
            return {**dict(row), 'details': json.loads(row['details'])}

    def confirmation(self, confirmation_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM confirmations WHERE id=?', (confirmation_id,)).fetchone()
            return dict(row) if row else None

    def apply_confirmed(self, session_id, confirmation_id, action, details):
        """Consome a aprovação e grava o efeito na mesma transação."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT c.*,b.apartment FROM confirmations c JOIN bindings b USING(session_id) WHERE c.id=? AND c.session_id=?',
                             (confirmation_id, session_id)).fetchone()
            if row is None or row['action'] != action or json.loads(row['details']) != details:
                return {'status': 'confirmacao_obrigatoria'}
            if row['result']: return json.loads(row['result'])
            if row['status'] != 'responding': return {'status': 'confirmacao_obrigatoria'}
            if not row['approved']:
                result = {'status': 'negada'}
            elif action == 'reservar_area':
                result = self._reserve(db, row['apartment'], details)
            elif action == 'autorizar_visitante':
                db.execute('INSERT INTO visitors(apartment,name,day) VALUES (?,?,?)', (row['apartment'], details['nome'], details['data']))
                result = {'status': 'autorizado', **details}
            else:
                raise ValueError('Acao de confirmacao desconhecida.')
            db.execute("UPDATE confirmations SET status='done',result=? WHERE id=?", (json.dumps(result), confirmation_id))
            return result

    def finish_denied(self, confirmation_id):
        with self.connection() as db:
            db.execute("UPDATE confirmations SET status='done',result=? WHERE id=? AND approved=0 AND status='responding'",
                       (json.dumps({'status': 'negada'}), confirmation_id))
