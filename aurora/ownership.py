"""Um runtime ou restauração por diretório; o SO libera a trava após crash."""
from pathlib import Path
import sqlite3


class DirectoryLease:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(directory / '.runtime.lock.db', timeout=0)
        try:
            # Banco separado: a transação não bloqueia reservas nem sessões.
            self.connection.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError:
            self.connection.close()
            raise RuntimeError('O diretorio de dados ja esta em uso por outro processo Aurora.') from None

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
