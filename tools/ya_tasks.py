#!/usr/bin/env python3
"""Запросы к API Яндекс Заданий с автоматическим журналом.

Каждый отправленный запрос получает номер NN и попадает:
  - в project/api_log.md: зачем, метод и URL, тело (коротко или ссылкой), статус и главное из ответа;
  - в project/api/NN_<имя>.json (тело) и NN_<имя>.response.json (ответ);
  - в project/api/log.jsonl (то же самое построчно, для сборки ноутбука).
Токен никуда не пишется.

По умолчанию ничего не отправляет (сухой прогон): печатает, что уйдёт.
Отправка только с флагом --send.

Примеры (запуск из папки project):
  python3 -I tools/ya_tasks.py whoami --send --why "проверить, что токен от командного аккаунта"
  python3 -I tools/ya_tasks.py GET pools/123 --send --why "посмотреть пул"
  python3 -I tools/ya_tasks.py POST projects --body configs/bbox_project.json --name create_bbox_project \
          --why "создать проект bbox" [--send]
  python3 -I tools/ya_tasks.py PUT pools/123 --body new_pool.json --base-file api/07_get_pool.response.json \
          --why "поменять цену" [--send]
  python3 -I tools/ya_tasks.py GET assignments --param pool_id=123 --param limit=1000 --paginate \
          --send --why "выгрузить ответы пилота"

Токен командного аккаунта: переменная YA_PROJECT_TOKEN (project/.env или корневой .env),
id командного заказчика: YA_PROJECT_REQUESTER_ID. Личный YA_TASK_TOKEN скрипт не читает никогда.
Перед каждым изменяющим запросом (POST/PUT/PATCH/DELETE) скрипт делает GET /requester
и отказывается работать, если id заказчика не совпал с YA_PROJECT_REQUESTER_ID.
"""
import argparse
import datetime
import difflib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException   # имя http занято функцией ниже
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
REAL_BASE = 'https://tasks.yandex.ru/api/v1/'
BASE = os.environ.get('YA_TASKS_BASE', REAL_BASE)          # подмена только для тестов на localhost
ROOT = Path(os.environ.get('YA_TASKS_LOG_ROOT', PROJECT))  # куда писать журнал (тесты пишут во временную папку)
API_DIR = ROOT / 'api'
LOG_MD = ROOT / 'api_log.md'
LOG_JSONL = API_DIR / 'log.jsonl'
MSK = datetime.timezone(datetime.timedelta(hours=3))
WRITE_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
INLINE_LIMIT = 600   # тело короче этого печатаем прямо в журнале
VOLATILE = {'created', 'last_started', 'last_stopped', 'last_close_reason', 'status', 'id', 'owner'}


def die(msg):
    sys.exit('ОШИБКА: ' + msg)


def is_real():
    return BASE == REAL_BASE


def credentials():
    """Токен и id заказчика командного аккаунта.

    Берём только YA_PROJECT_TOKEN и YA_PROJECT_REQUESTER_ID: сначала из project/.env, потом из корневого .env.
    Личный YA_TASK_TOKEN (ДЗ1–ДЗ2) не читаем никогда.
    """
    if not is_real():
        if not BASE.startswith(('http://127.0.0.1', 'http://localhost')):
            die('YA_TASKS_BASE можно подменять только на localhost (тесты)')
        return 'test-token', os.environ.get('YA_TASKS_TEST_REQUESTER', 'test-requester')
    vals = {}
    for env in (PROJECT.parent / '.env', PROJECT / '.env'):   # project/.env важнее корневого
        if env.exists():
            for line in env.read_text(encoding='utf-8').splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    k, v = line.split('=', 1)
                    if k.strip() in ('YA_PROJECT_TOKEN', 'YA_PROJECT_REQUESTER_ID'):
                        vals[k.strip()] = v.strip().strip('"\'')
    if not vals.get('YA_PROJECT_TOKEN'):
        die('нет YA_PROJECT_TOKEN ни в project/.env, ни в корневом .env')
    return vals['YA_PROJECT_TOKEN'], vals.get('YA_PROJECT_REQUESTER_ID', '')


def ssl_context():
    """Сертификаты из certifi, если он есть (у python.org-сборок на macOS своих нет), иначе системные."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def http(method, path, params=None, body=None, token=None):
    url = BASE + path.lstrip('/') + ('?' + urllib.parse.urlencode(params) if params else '')
    data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Authorization': 'OAuth ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=180, context=ssl_context() if url.startswith('https') else None) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, HTTPException, OSError) as e:   # сеть, обрыв, таймаут
        return None, {'_error': f'{type(e).__name__}: {e}'}
    text = raw.decode('utf-8', 'replace')
    try:
        return status, json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return status, {'_raw': text[:5000]}


def next_number():
    nums = [0]
    if API_DIR.exists():
        nums += [int(m.group(1)) for p in API_DIR.iterdir() if (m := re.match(r'^(\d+)_', p.name))]
    if LOG_MD.exists():
        nums += [int(n) for n in re.findall(r'^## (\d+)\.', LOG_MD.read_text(encoding='utf-8'), re.M)]
    return max(nums) + 1


def slug(method, path, name):
    s = name or (method.lower() + '_' + path.strip('/').replace('/', '_'))
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', s)[:60]


def write_new(path, text):
    """Создаёт файл; существующий никогда не перезаписывает."""
    with open(path, 'x', encoding='utf-8') as f:
        f.write(text)


def strip_volatile(obj):
    return {k: v for k, v in obj.items() if k not in VOLATILE} if isinstance(obj, dict) else obj


def show_diff(base_file, body):
    base = json.loads(Path(base_file).read_text(encoding='utf-8'))
    a = json.dumps(strip_volatile(base), ensure_ascii=False, indent=1, sort_keys=True).splitlines()
    b = json.dumps(strip_volatile(body), ensure_ascii=False, indent=1, sort_keys=True).splitlines()
    d = list(difflib.unified_diff(a, b, 'было (' + str(base_file) + ')', 'станет', lineterm='', n=2))
    print('\n'.join(d) if d else '(отличий нет, не считая служебных полей)')


def summary(resp):
    """Главное из ответа для журнала."""
    out = []
    if not isinstance(resp, dict):
        return out
    for k in ('id', 'status', 'private_name', 'public_name', 'balance', 'project_id', 'pool_id', 'skill_id',
              'reward_per_assignment', 'code', 'message', '_error'):
        if k in resp and not isinstance(resp[k], list):
            v = resp[k]
            out.append(f'`{k}`: ' + (json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else str(v)))
    if 'items' in resp and isinstance(resp['items'], list):
        out.append(f'записей: {len(resp["items"])}' + (f', has_more: {resp["has_more"]}' if 'has_more' in resp else ''))
    if 'pages' in resp:
        out.append(f'страниц: {resp["pages"]}')
    if 'payload' in resp and isinstance(resp['payload'], dict):
        out.append('payload: ' + json.dumps(resp['payload'], ensure_ascii=False)[:300])
    return out


def log_entry(n, why, method, path, params, body, body_file, status, resp_file, resp, check):
    ts = datetime.datetime.now(MSK).strftime('%Y-%m-%d %H:%M МСК')
    if not LOG_MD.exists():
        write_new(LOG_MD, '# Финальный проект: журнал запросов к API Яндекс Заданий\n\n'
                          'База: `https://tasks.yandex.ru/api/v1/`, заголовок `Authorization: OAuth <token>`.\n'
                          'Пункты пишет `tools/ya_tasks.py` автоматически; полные тела и ответы в `api/`.\n')
    q = ('?' + urllib.parse.urlencode(params)) if params else ''
    lines = [f'\n## {n:02d}. {why}\n', f'`{ts}`\n', '```', f'{method} /{path.lstrip("/")}{q}', '```\n']
    if body is not None:
        compact = json.dumps(body, ensure_ascii=False)
        if len(compact) <= INLINE_LIMIT:
            lines += ['**Тело:**\n', '```json', json.dumps(body, ensure_ascii=False, indent=1), '```\n']
        else:
            lines.append(f'**Тело:** [{body_file}]({body_file})\n')
    if check:
        lines.append(f'**Проверка аккаунта:** {check}\n')
    lines.append(f'**Ответ:** `{status if status is not None else "нет ответа"}`, полный ответ: [{resp_file}]({resp_file})')
    lines += ['- ' + s for s in summary(resp)]
    with open(LOG_MD, 'a', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    with open(LOG_JSONL, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'n': n, 'time': ts, 'why': why, 'method': method, 'path': path, 'params': params or {},
                            'body_file': body_file, 'status': status, 'response_file': resp_file,
                            'account_check': check}, ensure_ascii=False) + '\n')


def check_account(token, expected):
    status, resp = http('GET', 'requester', token=token)
    got = str(resp.get('id', '')) if isinstance(resp, dict) else ''
    if status != 200 or not got:
        die(f'GET /requester не удался ({status}): {json.dumps(resp, ensure_ascii=False)[:300]}')
    if not expected:
        die(f'нет YA_PROJECT_REQUESTER_ID в .env. Сейчас токен от заказчика id={got}. '
            f'Убедись, что это командный аккаунт, и впиши id в .env')
    if got != expected:
        die(f'токен от заказчика id={got}, а ожидается командный {expected}. Запрос НЕ отправлен')
    return f'GET /requester → id {got} совпал с командным'


def paginate(path, params, token):
    params = dict(params or {})
    params.setdefault('sort', 'id')
    items, pages, last = [], 0, None
    while True:
        p = dict(params, **({'id_gt': last} if last else {}))
        status, resp = http('GET', path, p, token=token)
        pages += 1
        if status != 200:
            return status, {'items': items, 'pages': pages, 'failed_page': resp}
        items += resp.get('items', [])
        if not resp.get('has_more') or not resp.get('items'):
            return status, {'items': items, 'pages': pages, 'has_more': False}
        last = resp['items'][-1]['id']


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('method', help='GET/POST/PUT/PATCH/DELETE или whoami')
    ap.add_argument('path', nargs='?', default='', help='путь после /api/v1/, например pools/123')
    ap.add_argument('--body', help='файл с JSON-телом')
    ap.add_argument('--json', help='JSON-тело строкой')
    ap.add_argument('--param', action='append', default=[], help='параметр запроса k=v (можно несколько)')
    ap.add_argument('--name', help='короткое имя для файлов в api/ (латиница)')
    ap.add_argument('--why', help='зачем делаем запрос (идёт в журнал)')
    ap.add_argument('--base-file', help='для PUT: последний GET этого объекта, покажу дифф')
    ap.add_argument('--paginate', action='store_true', help='для GET списков: собрать все страницы через id_gt')
    ap.add_argument('--send', action='store_true', help='реально отправить (иначе сухой прогон)')
    a = ap.parse_args()

    method = a.method.upper()
    if method == 'WHOAMI':
        method, a.path, a.name = 'GET', 'requester', a.name or 'whoami'
    if method not in WRITE_METHODS | {'GET'}:
        die('метод должен быть GET/POST/PUT/PATCH/DELETE или whoami')
    if not a.path:
        die('нужен путь, например pools/123')
    params = {}
    for kv in a.param:
        k, _, v = kv.partition('=')
        params[k] = v
    body = None
    if a.body and a.json:
        die('или --body, или --json')
    if a.body:
        body = json.loads(Path(a.body).read_text(encoding='utf-8'))
    elif a.json:
        body = json.loads(a.json)
    if method in {'POST', 'PUT', 'PATCH'} and body is None and not re.search(r'/(open|close|archive|clone)$', a.path):
        print('Внимание: изменяющий запрос без тела.')

    q = ('?' + urllib.parse.urlencode(params)) if params else ''
    print(f'{method} {BASE}{a.path.lstrip("/")}{q}' + ('   [ТЕСТОВЫЙ СЕРВЕР]' if not is_real() else ''))
    if body is not None:
        text = json.dumps(body, ensure_ascii=False, indent=1)
        cut = text.splitlines()
        print(f'Тело: {len(text)} символов' + (f', первые 60 строк из {len(cut)}:' if len(cut) > 60 else ':'))
        print('\n'.join(cut[:60]))
    if method == 'PUT':
        if a.base_file:
            print('\nДифф с последним GET:')
            show_diff(a.base_file, body)
        else:
            print('\nВнимание: PUT заменяет объект целиком. Передай --base-file с последним GET, чтобы увидеть дифф.')
    if a.paginate and method != 'GET':
        die('--paginate только для GET')

    if not a.send:
        print('\nСухой прогон: ничего не отправлено. Для отправки добавь --send.')
        return
    if not a.why:
        die('для отправки нужен --why (идёт в журнал)')

    token, expected = credentials()
    check = check_account(token, expected) if method in WRITE_METHODS else ''
    API_DIR.mkdir(parents=True, exist_ok=True)
    n = next_number()
    base = f'{n:02d}_{slug(method, a.path, a.name)}'
    body_file = None
    if body is not None:
        body_file = f'api/{base}.json'
        write_new(ROOT / body_file, json.dumps(body, ensure_ascii=False, indent=1))

    if a.paginate:
        status, resp = paginate(a.path, params, token)
    else:
        status, resp = http(method, a.path, params, body, token)
    resp_file = f'api/{base}.response.json'
    write_new(ROOT / resp_file, json.dumps(resp, ensure_ascii=False, indent=1))
    log_entry(n, a.why, method, a.path, params, body, body_file, status, resp_file, resp, check)

    print(f'\n№{n:02d}: статус {status}, ответ в {resp_file}, запись в api_log.md')
    for s in summary(resp):
        print('  ' + s.replace('`', ''))
    if status is None and method == 'POST' and a.path.strip('/').startswith('tasks'):
        print('Нет ответа на POST /tasks. НЕ повторяй: задания могли залиться. Подожди минуту и посчитай GET tasks?pool_id=…')
    if status == 504 and method == 'POST':
        print('504 на POST: задания могли залиться с задержкой. НЕ повторяй, проверь через GET.')
    if status is None or status >= 400:
        sys.exit(1)


if __name__ == '__main__':
    main()
