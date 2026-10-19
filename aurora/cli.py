import os
from pathlib import Path

from dotenv import load_dotenv
import uvicorn

from .store import Store
from .ownership import DirectoryLease


def serve():
    load_dotenv()
    uvicorn.run('aurora.api:create_app', factory=True, host='127.0.0.1', port=8000)


def reset():
    load_dotenv()
    directory = Path(os.getenv('AURORA_DATA_DIR') or '.data')
    # A trava impede restaurar enquanto a API usa qualquer um dos dois bancos.
    with DirectoryLease(directory):
        for name in ('sessions.db', 'sessions.db-wal', 'sessions.db-shm', 'condominio.db', 'condominio.db-wal', 'condominio.db-shm'):
            (directory / name).unlink(missing_ok=True)
        Store(directory / 'condominio.db')
    print('Dados iniciais restaurados; sessoes e pendencias removidas.')
