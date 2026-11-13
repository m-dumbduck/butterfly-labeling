#!/usr/bin/env python3
"""Проверка tools/ya_tasks.py на локальном фальшивом сервере. Настоящий API не трогает.

Запуск из папки project: python3 -I tools/test_ya_tasks.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRIPT = Path(__file__).resolve().parent / 'ya_tasks.py'
SEEN = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(raw)

    def handle_any(self, method):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        SEEN.append((method, u.path, q, self.headers.get('Authorization')))
        p = u.path.replace('/api/v1/', '')
        if p == 'requester':
            return self.reply(200, {'id': 'team-42', 'public_name': 'Команда'})
        if method == 'POST' and p == 'projects':
            return self.reply(201, dict(body, id='777', status='ACTIVE'))
        if method == 'POST' and p == 'pools':
            return self.reply(400, {'code': 'VALIDATION_ERROR', 'message': 'bad pool'})
        if method == 'GET' and p == 'assignments':
            start = int(q.get('id_gt', 0))
            items = [{'id': i} for i in range(start + 1, min(start + 3, 6) + 1)]
            return self.reply(200, {'items': items, 'has_more': items[-1]['id'] < 6})
        self.reply(404, {'code': 'NOT_FOUND'})

    def do_GET(self):
        self.handle_any('GET')

    def do_POST(self):
        self.handle_any('POST')

    def do_PUT(self):
        self.handle_any('PUT')


def run(args, root, port, requester='team-42'):
    env = dict(os.environ, YA_TASKS_BASE=f'http://127.0.0.1:{port}/api/v1/', YA_TASKS_LOG_ROOT=str(root),
               YA_TASKS_TEST_REQUESTER=requester)
    return subprocess.run([sys.executable, '-I', str(SCRIPT)] + args, env=env, capture_output=True, text=True)


def main():
    srv = HTTPServer(('127.0.0.1', 0), Fake)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ok = True

    def check(cond, what):
        nonlocal ok
        print(('OK   ' if cond else 'FAIL ') + what)
        ok &= bool(cond)

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        big = root / 'project_body.json'
        big.write_text(json.dumps({'public_name': 'Бабочки', 'task_spec': {'x': 'y' * 800}}, ensure_ascii=False))

        r = run(['POST', 'projects', '--body', str(big)], root, port)
        check(r.returncode == 0 and 'Сухой прогон' in r.stdout and not (root / 'api').exists() and not SEEN,
              'сухой прогон ничего не отправляет и не пишет')

        r = run(['POST', 'projects', '--body', str(big), '--send'], root, port)
        check(r.returncode != 0 and '--why' in r.stderr and not SEEN, 'без --why отправка запрещена')

        r = run(['whoami', '--send', '--why', 'проверить аккаунт'], root, port)
        check(r.returncode == 0 and (root / 'api/01_whoami.response.json').exists(), 'whoami → №01')

        r = run(['POST', 'projects', '--body', str(big), '--name', 'create_bbox_project', '--send',
                 '--why', 'создать проект bbox'], root, port)
        log = (root / 'api_log.md').read_text(encoding='utf-8')
        check(r.returncode == 0 and (root / 'api/02_create_bbox_project.json').exists()
              and '## 02. создать проект bbox' in log and '[api/02_create_bbox_project.json]' in log
              and '`id`: 777' in log and 'совпал с командным' in log, 'POST с длинным телом → №02, тело ссылкой, id в журнале')
        check(all(a == 'OAuth test-token' for *_, a in SEEN), 'на тестовый сервер уходит только тестовый токен')

        before = len(SEEN)
        r = run(['POST', 'projects', '--json', '{"a": 1}', '--send', '--why', 'чужой аккаунт'], root, port,
                requester='team-OTHER')
        check(r.returncode != 0 and 'НЕ отправлен' in r.stderr and len(SEEN) == before + 1
              and SEEN[-1][1].endswith('/requester'), 'чужой заказчик: только GET /requester, сам POST не ушёл')

        r = run(['POST', 'pools', '--json', '{"project_id": "777"}', '--send', '--why', 'ошибка валидации'], root, port)
        log = (root / 'api_log.md').read_text(encoding='utf-8')
        check(r.returncode == 1 and '## 03. ошибка валидации' in log and 'VALIDATION_ERROR' in log
              and '"project_id": "777"' in log, 'ошибка 400 записана в журнал, короткое тело прямо в журнале')

        r = run(['GET', 'assignments', '--param', 'pool_id=5', '--paginate', '--send', '--why', 'ответы'], root, port)
        resp = json.loads((root / 'api/04_get_assignments.response.json').read_text(encoding='utf-8'))
        check(r.returncode == 0 and [x['id'] for x in resp['items']] == [1, 2, 3, 4, 5, 6] and resp['pages'] == 2,
              'пагинация собрала 6 записей за 2 страницы')

        base = root / 'pool_get.json'
        base.write_text(json.dumps({'id': '1', 'status': 'OPEN', 'reward_per_assignment': 0.5, 'may_contain_adult_content': True}))
        new = root / 'pool_put.json'
        new.write_text(json.dumps({'reward_per_assignment': 0.6, 'may_contain_adult_content': True}))
        r = run(['PUT', 'pools/1', '--body', str(new), '--base-file', str(base)], root, port)
        check('- "reward_per_assignment": 0.5' in r.stdout and '+ "reward_per_assignment": 0.6' in r.stdout
              and 'status' not in r.stdout.split('Дифф')[1], 'PUT: дифф показывает только цену')

        (root / 'api/09_manual.json').write_text('{}')
        r = run(['whoami', '--send', '--why', 'после ручного файла'], root, port)
        check((root / 'api/10_whoami.response.json').exists(), 'нумерация продолжается после чужих файлов, ничего не перезаписано')
        jl = [json.loads(x) for x in (root / 'api/log.jsonl').read_text(encoding='utf-8').splitlines()]
        check([x['n'] for x in jl] == [1, 2, 3, 4, 10], 'log.jsonl: по строке на каждый отправленный запрос')
        full = (root / 'api_log.md').read_text(encoding='utf-8')
        check('test-token' not in full and all('test-token' not in p.read_text(encoding='utf-8')
                                               for p in (root / 'api').iterdir()), 'токена нет ни в журнале, ни в файлах')
        print('\n--- пример журнала ---\n' + full[:1500])
    srv.shutdown()
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
