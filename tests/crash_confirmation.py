"""Processo auxiliar dos testes: encerra abruptamente dentro da tool confirmada."""
import asyncio
import os
from pathlib import Path
import sys

from aurora.runtime import Runtime
from tests.fake_model import ScriptedModel


async def main():
    directory, session_id, confirmation_id, moment = sys.argv[1:]
    model = ScriptedModel()
    runtime = Runtime(Path(directory), model=model)
    original = runtime.store.apply_confirmed

    def interrupted(*args):
        if moment == 'before':
            os._exit(86)
        original(*args)
        os._exit(87)

    runtime.store.apply_confirmed = interrupted
    await runtime.confirm(session_id, confirmation_id, True)
    raise AssertionError('O processo deveria ter sido encerrado dentro da tool.')


if __name__ == '__main__':
    asyncio.run(main())
