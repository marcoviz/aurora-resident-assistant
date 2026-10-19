"""Smoke do processo HTTP real, sem chave Gemini. Nao substitui teste do modelo."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(sys.executable).parent
URL = 'http://127.0.0.1:8000'


def request(path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = Request(URL + path, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urlopen(req, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def start(env, log):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('127.0.0.1', 8000))
    process = subprocess.Popen([str(BIN / 'aurora-api')], cwd=ROOT, env=env, stdout=log, stderr=log)
    for _ in range(150):
        if process.poll() is not None:
            raise AssertionError('Processo encerrou antes de iniciar; verifique a porta 8000.')
        try:
            if request('/apartamentos/101/reservas')[0] == 200:
                return process
        except (URLError, TimeoutError):
            time.sleep(.1)
    process.terminate()
    process.wait(timeout=5)
    raise AssertionError('API nao iniciou.')


def stop(process):
    process.send_signal(signal.SIGINT)
    process.wait(timeout=10)


def main():
    process = None
    with TemporaryDirectory(prefix='aurora-http-') as folder:
        env = dict(os.environ, AURORA_DATA_DIR=folder, GOOGLE_API_KEY='')
        subprocess.run([str(BIN / 'aurora-reset')], cwd=ROOT, env=env, check=True)
        with open(Path(folder) / 'http.log', 'w') as log:
            try:
                process = start(env, log)
                assert request('/apartamentos/101/reservas') == (200, [{'codigo':'RSV-1377','area':'quadra','data':'2030-03-09'}])
                print('PASS HTTP sementes restauradas pelo CLI')
                code, result = request('/sessoes', {'apartamento':'101'})
                assert code == 201
                session = result['session_id']
                assert request(f'/sessoes/{session}/eventos') == (200, [])
                print('PASS HTTP sessao criada e eventos consultaveis')
                assert request(f'/sessoes/{session}/mensagens', {'texto':'Ola'})[0] == 503
                print('PASS HTTP ausencia de chave retorna 503, sem resposta simulada')
                assert request(f'/sessoes/{session}/confirmacoes', {'id':'inexistente','confirmado':True})[0] == 409
                assert request('/sessoes/inexistente/eventos')[0] == 404
                print('PASS HTTP contratos de erro 409 e 404')
                stop(process)
                process = None
                process = start(env, log)
                assert request(f'/sessoes/{session}/eventos') == (200, [])
                assert request('/apartamentos/302/visitantes') == (200, [{'nome':'Marina Duarte','data':'2030-03-16'}])
                print('PASS HTTP processo reiniciado com sessao e dados persistidos')
                print('5/5 smoke HTTP PASS; Gemini nao foi chamado.')
            finally:
                if process is not None and process.poll() is None:
                    stop(process)


if __name__ == '__main__':
    main()
