#!/usr/bin/env python3
"""Проверка tools/qc_service.py на локальном фальшивом сервере. Настоящий API не трогает.

Запуск из папки project: python3 -I tools/test_qc_service.py
"""
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parents[1]
ASSIGNMENTS, WRITES, GETS, FAILED = [], [], [], []
FAIL_ONCE = set()   # (метод, путь, user_id): первый такой запрос получит 500


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path.replace('/api/v1/', '')
        if p == 'requester':
            return self.reply(200, {'id': 'team-42'})
        if p == 'assignments':
            GETS.append(q)
            items = [a for a in ASSIGNMENTS if a['pool_id'] == q.get('pool_id') and a['status'] == q.get('status', a['status'])
                     and a['user_id'] == q.get('user_id', a['user_id']) and a['id'] > q.get('id_gt', '')
                     and a.get('submitted', '') >= q.get('submitted_gte', '')]
            return self.reply(200, {'items': sorted(items, key=lambda a: a['id']), 'has_more': False})
        self.reply(404, {})

    def do_PUT(self):
        p = urlparse(self.path).path.replace('/api/v1/', '')
        b = self.body()
        if ('PUT', p, b.get('user_id')) in FAIL_ONCE:
            FAIL_ONCE.discard(('PUT', p, b.get('user_id')))
            FAILED.append(('PUT', p, b))
            return self.reply(500, {'code': 'INTERNAL_ERROR'})
        WRITES.append(('PUT', p, b))
        self.reply(201, {'id': f'w{len(WRITES)}'})

    def do_PATCH(self):
        p = urlparse(self.path).path.replace('/api/v1/', '')
        b = self.body()
        WRITES.append(('PATCH', p, b))
        for a in ASSIGNMENTS:
            if p == f"assignments/{a['id']}":
                a['status'] = b['status']
        self.reply(200, {'id': p.split('/')[-1], 'status': b['status']})


def main():
    ok = True

    def check(cond, what):
        nonlocal ok
        print(('OK   ' if cond else 'FAIL ') + what)
        ok &= bool(cond)

    rows = json.load(open(HERE / 'control' / 'control_reference_v1.json', encoding='utf-8'))['rows']
    exam = [r for r in rows if r['role'] == 'exam' and r['count'] != '0']
    honey = [r for r in rows if r['role'] == 'honeypot' and r['count'] != '0']
    reference = {r['photo_id']: {'count': r['count'], 'boxes': r['boxes']} for r in exam + honey}
    rng = random.Random(3)
    rb = lambda: {'left': rng.uniform(0, .6), 'top': rng.uniform(0, .6), 'width': .1, 'height': .1}
    sol = lambda boxes, none=False, err=False: {'output_values': {'boxes': boxes, 'no_butterflies': none, 'load_error': err}}

    def exam_page(i, user, mode):
        s = [sol(r['maksim_boxes']) if mode == 'good' else
             sol([rb() for _ in r['boxes']]) if mode == 'cheat' else sol([], none=True) for r in exam]
        return {'id': f'e{i}', 'pool_id': 'EXAM', 'user_id': user, 'status': 'ACCEPTED', 'submitted': f'2026-11-14T10:00:{i:02d}',
                'tasks': [{'input_values': {'photo_id': r['photo_id']}} for r in exam], 'solutions': s}

    def bbox_page(i, user, hp, good, err=False):
        tasks = [{'input_values': {'photo_id': f'x{i}{k}'}} for k in range(3)] + [{'input_values': {'photo_id': hp['photo_id']}}]
        s = [sol([rb()]) for _ in range(3)] + [sol([], err=True) if err else sol(hp['boxes'] if good else [rb()])]
        return {'id': f'b{i}', 'pool_id': 'BBOX', 'user_id': user, 'status': 'SUBMITTED', 'submitted': f'2026-11-14T10:01:{i:02d}',
                'tasks': tasks, 'solutions': s}

    def exam_page_errors(i, user, n_err):
        s = [sol([rb()] * len(r['boxes'])) if k < n_err else sol(r['maksim_boxes']) for k, r in enumerate(exam)]
        return {'id': f'e{i}', 'pool_id': 'EXAM', 'user_id': user, 'status': 'ACCEPTED', 'submitted': f'2026-11-14T10:00:{i:02d}',
                'tasks': [{'input_values': {'photo_id': r['photo_id']}} for r in exam], 'solutions': s}

    ASSIGNMENTS.extend([exam_page_errors(4, 'u_one_err', 1), exam_page_errors(5, 'u_two_err', 2)])
    ASSIGNMENTS.extend([exam_page(1, 'u_good', 'good'), exam_page(2, 'u_cheat', 'cheat'), exam_page(3, 'u_lazy', 'lazy'),
                    bbox_page(1, 'u_ok', honey[0], True), bbox_page(2, 'u_ok', honey[1], True),
                    bbox_page(3, 'u_bad', honey[2], False), bbox_page(4, 'u_bad', honey[3], True), bbox_page(5, 'u_bad', honey[4], True),
                        bbox_page(8, 'u_net', honey[6], False, err=True)])
    ASSIGNMENTS.append({'id': 'b6', 'pool_id': 'BBOX', 'user_id': 'u_ok', 'status': 'ACTIVE', 'tasks': [], 'solutions': []})

    srv = HTTPServer(('127.0.0.1', 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / 'ref.json').write_text(json.dumps(reference))
        cfg = {'reference': str(d / 'ref.json'), 'iou': 0.65, 'poll_seconds': 1, 'pools': [
            {'type': 'exam', 'pool_id': 'EXAM', 'skill_id': 'S1', 'max_failed_photos': 1, 'pass_value': 100, 'fail_value': 0},
            {'type': 'bbox', 'pool_id': 'BBOX', 'project_id': 'P1', 'author_skill_id': 'AUTH', 'ban_after': 1, 'ban_days': 30, 'reject_failed_page': True,
             'max_pages_per_user': 1}]}
        (d / 'cfg.json').write_text(json.dumps(cfg))
        env = dict(os.environ, YA_TASKS_BASE=f'http://127.0.0.1:{srv.server_address[1]}/api/v1/', YA_TASKS_LOG_ROOT=str(d),
                   YA_TASKS_TEST_REQUESTER='team-42')
        run = lambda *extra: subprocess.run([sys.executable, '-I', str(HERE / 'tools' / 'qc_service.py'), '--config', str(d / 'cfg.json'),
                                             '--once', *extra], env=env, capture_output=True, text=True)

        r = run()
        check(r.returncode == 0 and not WRITES and not (d / 'api' / 'qc_state.json').exists() and '[сухо]' in r.stdout,
              'сухой режим: изменяющих запросов нет, состояние не сохранено')
        if r.returncode:
            print(r.stdout, r.stderr)

        FAIL_ONCE.add(('PUT', 'user-skills', 'u_net'))   # сбой API на «авторе» для u_net: страницу надо обработать ещё раз
        r = run('--live')
        print(r.stdout.strip())
        skills = {w[2]['user_id']: w[2]['value'] for w in WRITES if w[1] == 'user-skills' and w[2]['skill_id'] == 'S1'}
        authors = sorted(w[2]['user_id'] for w in WRITES if w[1] == 'user-skills' and w[2]['skill_id'] == 'AUTH')
        check(authors == ['u_bad', 'u_ok'] and len(FAILED) == 1, 'навык «автор» получили все, кто сдал страницу обводки (у u_net сбой API)')
        check(skills == {'u_good': 100, 'u_cheat': 0, 'u_lazy': 0, 'u_one_err': 100, 'u_two_err': 0},
              'экзамен: аккуратный и «одна ошибка» сдали; «две ошибки», халтурщик и «везде 0» не сдали')
        rejected = sorted(w[1].split('/')[1] for w in WRITES if w[0] == 'PATCH')
        bans = [w[2]['user_id'] for w in WRITES if w[1] == 'user-restrictions' and w[2]['scope'] == 'PROJECT']
        check(rejected == ['b3', 'b4', 'b5'] and bans == ['u_bad'],
              'обводка: бан после первого проваленного ханипота, его страница и остальные его страницы отклонены по одному разу')
        check(not any(w[1].endswith(('b1', 'b2')) for w in WRITES), 'нормального исполнителя не тронули')
        check(not any(w[1].endswith('b8') or (w[1] == 'user-restrictions' and w[2]['user_id'] == 'u_net' and w[2]['scope'] == 'PROJECT')
                      for w in WRITES),
              '«Картинка не загрузилась» на ханипоте: ни отклонения, ни бана')
        limits = sorted(w[2]['user_id'] for w in WRITES if w[1] == 'user-restrictions' and w[2]['scope'] == 'POOL')
        check(limits == ['u_net', 'u_ok'], 'лимит 1 страница: запрет на пул после первой страницы, у забаненного запрета на пул нет')
        log = (d / 'api_log.md').read_text(encoding='utf-8')
        check(log.count('## ') == 14 and 'сервис КК: блокировка' in log and 'совпал с командным' in log,
              'каждое из 14 изменяющих действий (5 навыков экзамена, 3 «автора» вкл. неудачный, бан, 3 отклонения, 2 лимита) в api_log.md')
        check(list((d / 'api' / 'qc_requests').glob('*.jsonl')) and (d / 'api' / 'qc_decisions.jsonl').exists()
              and (d / 'api' / 'qc_heartbeat.json').exists(), 'GET-опросы, решения и пульс записаны в свои файлы')

        before = len(WRITES)
        r = run('--live')
        new = WRITES[before:]
        check(r.returncode == 0 and [(w[1], w[2]['user_id']) for w in new] == [('user-skills', 'u_net')],
              'повторный проход: дослан только «автор» для u_net после сбоя, лимит и счётчики не задвоились')
        before = len(WRITES)
        r = run('--live')
        check(r.returncode == 0 and len(WRITES) == before, 'третий проход: никаких новых действий')
        ASSIGNMENTS.append(bbox_page(7, 'u_bad', honey[5], True))
        r = run('--live')
        check(len(WRITES) == before + 1 and WRITES[-1][1] == 'assignments/b7'
              and len([w for w in WRITES if w[1] == 'user-restrictions' and w[2]['scope'] == 'PROJECT']) == 1,
              'новая страница уже заблокированного: отклонена, повторного бана нет')

        # бесконечный цикл: сначала сухой режим по конфигу, новая страница, потом "live": true в конфиге на лету
        ASSIGNMENTS.append(bbox_page(9, 'u_new', honey[7], True))
        before, gets_before = len(WRITES), len(GETS)
        cfg.update(poll_seconds=0.2, live=False)
        (d / 'cfg.json').write_text(json.dumps(cfg))
        proc = subprocess.Popen([sys.executable, '-I', str(HERE / 'tools' / 'qc_service.py'), '--config', str(d / 'cfg.json')],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(2.5)
        dry_writes = len(WRITES) - before
        cfg['live'] = True
        (d / 'cfg.json').write_text(json.dumps(cfg))
        time.sleep(2.5)
        proc.terminate()
        out = proc.communicate(timeout=10)[0]
        new = [(w[1], w[2]['user_id']) for w in WRITES[before:]]
        check(dry_writes == 0 and out.count('[сухо] PUT /user-skills') == 1,
              'цикл в сухом режиме: страницу показал один раз, а не на каждом опросе, ничего не отправил')
        check('конфиг перечитан: БОЕВОЙ' in out and new == [('user-skills', 'u_new'), ('user-restrictions', 'u_new')],
              'правка конфига на лету: перешёл в боевой режим и обработал страницу (автор + лимит)')
        check(any('submitted_gte' in q for q in GETS[gets_before:]), 'в цикле новые страницы забираются по времени сдачи (submitted_gte)')
        if not ('конфиг перечитан: БОЕВОЙ' in out):
            print(out)
    srv.shutdown()
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
