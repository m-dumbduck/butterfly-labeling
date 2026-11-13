#!/usr/bin/env python3
"""Сервис контроля качества обводки (bbox): следит за пулами и реагирует на сданные страницы за пару секунд.

Запуск из папки project:
  python3 -I tools/qc_service.py --config configs/qc_service.json            # бесконечный цикл, режим из конфига
  python3 -I tools/qc_service.py --config configs/qc_service.json --once     # один проход
  python3 -I tools/qc_service.py --config configs/qc_service.json --live     # боевой режим независимо от конфига
Боевой режим (реально выдаёт навык / блокирует / отклоняет): флаг --live или "live": true в конфиге. Иначе сухой:
изменяющие запросы только печатаются.
Пауза: создать файл api/qc_STOP (цикл ничего не делает, пока файл есть; с --once просто выходит).
Конфиг и эталон перечитываются на лету, если файл поменялся. Если поменялся код сервиса (git pull),
сервис перезапускает сам себя.

Что делает за проход (раз в poll_seconds, по умолчанию 2 с):
  экзамен (type "exam"): по каждой новой сданной странице считает рамки против эталона (IoU ≥ iou,
    максимальное паросочетание) и выдаёт навык (PUT /user-skills): pass_value или fail_value.
    С max_failed_photos: сдал, если фото с ошибкой (пропущенная или лишняя рамка) не больше max_failed_photos.
    Без него: F1 ≥ min_f1 и (необязательно) доля фото с верным числом рамок ≥ min_count_ratio.
  обводка (type "bbox"): каждому, кто сдал страницу, ставит навык «автор» (author_skill_id, если задан);
    по каждой новой сданной странице проверяет ханипоты (фото из эталона).
    Проваленный ханипот = на нём есть пропущенная или лишняя рамка: страница отклоняется (PATCH /assignments/{id}),
    если reject_failed_page. Ханипот с отметкой «Картинка не загрузилась» провалом не считается (только в журнал).
    После ban_after провалов: блокировка в проекте (PUT /user-restrictions) и отклонение всех его ещё не принятых страниц.
    max_pages_per_user: после стольких сданных страниц запрет на этот пул (встроенное правило платформы считает
    только принятые страницы, а у нас отложенная приёмка).
Новые страницы забираются по времени сдачи (submitted_gte с запасом), раз в full_sweep_seconds полный обход.
Если изменяющий запрос не прошёл, страница обрабатывается снова в следующем проходе (не больше MAX_TRIES раз);
повторных навыков, банов и отклонений не будет: что уже сделано, записано в состоянии.
Журналы:
  каждое изменяющее действие: отдельный пункт в api_log.md + api/NN_*.json (как у ya_tasks.py);
  каждый GET опроса: api/qc_requests/ГГГГ-ММ-ДД.jsonl (коротко); решения по страницам: api/qc_decisions.jsonl;
  состояние: api/qc_state.json; пульс (последний проход, режим, ошибки): api/qc_heartbeat.json.
"""
import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ya_tasks  # noqa: E402
from boxmetrics import counts, f1  # noqa: E402

API = ya_tasks.API_DIR
STATE_F, DECISIONS_F, STOP_F, BEAT_F = API / 'qc_state.json', API / 'qc_decisions.jsonl', API / 'qc_STOP', API / 'qc_heartbeat.json'
GETLOG_DIR = API / 'qc_requests'
COMMENT_FAIL = ('Рамки на проверочном фото не совпали с правильным ответом: пропущена бабочка, лишняя рамка '
                'или рамка сильно не по размеру. Перечитайте инструкцию.')
CODE = [Path(__file__).resolve()] + [Path(__file__).resolve().parent / n for n in ('ya_tasks.py', 'boxmetrics.py')]
MAX_TRIES = 3          # сколько раз пробовать страницу, если изменяющий запрос не прошёл
OVERLAP = 300          # с каким запасом (с) брать страницы по времени сдачи


def now():
    return datetime.datetime.now(ya_tasks.MSK).strftime('%Y-%m-%d %H:%M:%S')


def jl(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(obj, ensure_ascii=False) + '\n')


def mtime(p):
    try:
        return Path(p).stat().st_mtime
    except OSError:
        return None


class Service:
    def __init__(self, cfg_path, cli_live):
        self.cfg_path, self.cli_live = Path(cfg_path), cli_live
        self.token, expected = ya_tasks.credentials()
        self.account = ya_tasks.check_account(self.token, expected)   # падает, если токен не командный
        self.reset_state()
        self.since = {}            # (pool, status) → самое позднее время сдачи, которое видели
        self.last_full = {}        # pool → когда был полный обход
        self.no_filter = False     # API не понял submitted_gte → всегда полный обход
        self.code_mtimes = [mtime(p) for p in CODE]
        self.cfg_mtime = self.ref_mtime = None
        self.load_config()
        self.backoff, self.loops, self.last_error = 0, 0, None
        print(f'{now()} старт, {"БОЕВОЙ" if self.live else "сухой"} режим, опрос раз в {self.cfg.get("poll_seconds", 2)} с, '
              f'пулов {len(self.cfg["pools"])}, {self.account}', flush=True)

    def reset_state(self):
        """Состояние с диска; то, что насчитано в сухом режиме, сюда не попадает (на диск пишем только в боевом)."""
        self.state = json.load(open(STATE_F, encoding='utf-8')) if STATE_F.exists() else {}
        for k in ('processed', 'users', 'banned', 'graded', 'limited'):
            self.state.setdefault(k, {})
        self.seen = set()          # обработано в этом запуске (и в сухом режиме, чтобы не печатать одно и то же каждые 2 с)
        self.tries = {}            # assignment id → сколько раз не прошли изменяющие запросы

    # --- конфиг и код на лету ---
    def load_config(self):
        self.cfg = json.load(open(self.cfg_path, encoding='utf-8'))
        self.cfg.setdefault('pools', [])
        self.cfg_mtime = mtime(self.cfg_path)
        ref = self.cfg.get('reference')
        self.ref_path = (ya_tasks.PROJECT / ref) if ref else None
        self.reference = json.load(open(self.ref_path, encoding='utf-8')) if ref else {}
        self.ref_mtime = mtime(self.ref_path) if ref else None
        self.live = self.cli_live or self.cfg.get('live') is True

    def reload_if_changed(self):
        if [mtime(p) for p in CODE] != self.code_mtimes:
            print(f'{now()} код сервиса поменялся, перезапускаюсь', flush=True)
            os.execv(sys.executable, [sys.executable] + sys.orig_argv[1:] if hasattr(sys, 'orig_argv') else [sys.executable] + sys.argv)
        if mtime(self.cfg_path) != self.cfg_mtime or (self.ref_path and mtime(self.ref_path) != self.ref_mtime):
            was = self.live
            try:
                self.load_config()
            except (OSError, json.JSONDecodeError) as e:   # недописанный файл: попробуем в следующий раз
                print(f'{now()} конфиг не читается ({e}), оставляю прежний', flush=True)
                return
            if was != self.live:   # сухой ↔ боевой: начинаем с состояния на диске, всё необработанное пройдём заново
                self.reset_state()
            print(f'{now()} конфиг перечитан: {"БОЕВОЙ" if self.live else "сухой"} режим, пулов {len(self.cfg["pools"])}, '
                  f'эталон {len(self.reference)} фото' + (' (режим поменялся)' if was != self.live else ''), flush=True)

    def save(self):
        API.mkdir(parents=True, exist_ok=True)
        tmp = STATE_F.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(STATE_F)

    def beat(self, paused=False):
        API.mkdir(parents=True, exist_ok=True)
        tmp = BEAT_F.with_suffix('.tmp')
        tmp.write_text(json.dumps({'time': now(), 'pid': os.getpid(), 'live': self.live, 'paused': paused, 'loops': self.loops,
                                   'pools': [p['pool_id'] for p in self.cfg['pools']], 'backoff': self.backoff,
                                   'last_error': self.last_error, 'processed': {k: len(v) for k, v in self.state['processed'].items()}},
                                  ensure_ascii=False), encoding='utf-8')
        tmp.replace(BEAT_F)

    # --- запросы ---
    def get_all(self, path, params):
        """Все страницы выдачи; None, если запрос не удался."""
        items, last = [], None
        while True:
            p = dict(params, sort='id', limit=200, **({'id_gt': last} if last else {}))
            t0 = time.monotonic()
            status, resp = ya_tasks.http('GET', path, p, token=self.token)
            n = len(resp.get('items', [])) if isinstance(resp, dict) else None
            jl(GETLOG_DIR / f"{datetime.datetime.now(ya_tasks.MSK):%Y-%m-%d}.jsonl",
               {'t': now(), 'path': path, 'q': {k: v for k, v in p.items() if k not in ('sort', 'limit')}, 'st': status, 'n': n,
                'ms': round((time.monotonic() - t0) * 1000)})
            if status != 200:
                self.last_error = f'{now()} GET {path} {p}: {status} {json.dumps(resp, ensure_ascii=False)[:200]}'
                print(self.last_error, flush=True)
                return None, status
            items += resp.get('items', [])
            if not resp.get('has_more') or not resp.get('items'):
                return items, status
            last = resp['items'][-1]['id']

    def write(self, method, path, body, why, name):
        """Изменяющий запрос: в сухом режиме только печать; в боевом через журнал как у ya_tasks.py."""
        if not self.live:
            print(f'{now()} [сухо] {method} /{path} {json.dumps(body, ensure_ascii=False)[:160]}  ({why})', flush=True)
            return True
        n = ya_tasks.next_number()
        base = f'{n:02d}_{name}'
        body_file = f'api/{base}.json'
        ya_tasks.write_new(ya_tasks.ROOT / body_file, json.dumps(body, ensure_ascii=False, indent=1))
        status, resp = ya_tasks.http(method, path, None, body, self.token)
        resp_file = f'api/{base}.response.json'
        ya_tasks.write_new(ya_tasks.ROOT / resp_file, json.dumps(resp, ensure_ascii=False, indent=1))
        ya_tasks.log_entry(n, 'сервис КК: ' + why, method, path, None, body, body_file, status, resp_file, resp, self.account)
        print(f'{now()} №{n:02d} {method} /{path}: {status}  ({why})', flush=True)
        ok = status is not None and status < 400
        if not ok:
            self.last_error = f'{now()} №{n:02d} {method} /{path}: {status}'
        return ok

    def new_assignments(self, pool, statuses):
        """Новые сданные страницы пула. None, если опрос не удался."""
        pid = pool['pool_id']
        done = set(self.state['processed'].get(pid, [])) | self.seen
        t = time.monotonic()
        full = self.no_filter or t - self.last_full.get(pid, -1e9) >= self.cfg.get('full_sweep_seconds', 600)
        out = []
        for st in statuses:
            params = {'pool_id': pid, 'status': st}
            since = self.since.get((pid, st))
            if not full and since:
                params['submitted_gte'] = (since - datetime.timedelta(seconds=OVERLAP)).strftime('%Y-%m-%dT%H:%M:%S')
            items, status = self.get_all('assignments', params)
            if items is None and status == 400 and 'submitted_gte' in params:
                print(f'{now()} API не принял submitted_gte, дальше только полный обход', flush=True)
                self.no_filter = True
                items, status = self.get_all('assignments', {'pool_id': pid, 'status': st})
            if items is None:
                return None
            for a in items:
                if a.get('submitted'):
                    s = datetime.datetime.fromisoformat(a['submitted'].replace('Z', '')[:19])
                    if (pid, st) not in self.since or s > self.since[(pid, st)]:
                        self.since[(pid, st)] = s
            out += [a for a in items if a['id'] not in done and a.get('solutions')]
        if full:
            self.last_full[pid] = t
        return sorted(out, key=lambda a: a['id'])

    def mark(self, pool, a, ok=True):
        """Страница обработана. Если что-то не отправилось, попробуем ещё раз в следующем проходе (до MAX_TRIES)."""
        if not ok:
            self.tries[a['id']] = self.tries.get(a['id'], 0) + 1
            if self.live:   # то, что всё-таки ушло (навык, бан, лимит), запоминаем сразу, чтобы не отправить второй раз
                self.save()
            if self.tries[a['id']] < MAX_TRIES:
                return
            print(f"{now()} страница {a['id']}: запросы не прошли {MAX_TRIES} раза, больше не пробую (см. api_log.md)", flush=True)
        self.seen.add(a['id'])
        done = self.state['processed'].setdefault(pool['pool_id'], [])
        if self.live and a['id'] not in done:   # в сухом режиме не запоминаем, чтобы боевой запуск обработал всё заново
            done.append(a['id'])
            self.save()

    # --- проверки ---
    def check_tasks(self, a):
        """По эталонным фото страницы: список (photo_id, count_ok, tp, fp, fn, load_error)."""
        res = []
        for t, s in zip(a.get('tasks', []), a.get('solutions', [])):
            pid = str(t['input_values'].get('photo_id'))
            if pid in self.reference:
                ref, out = self.reference[pid], s.get('output_values', {})
                boxes = out.get('boxes') or []
                tp, fp, fn = counts(ref['boxes'], boxes, self.cfg.get('iou', 0.65))
                res.append((pid, len(boxes) == len(ref['boxes']), tp, fp, fn, bool(out.get('load_error'))))
        return res

    def do_exam(self, pool):
        new = self.new_assignments(pool, ('SUBMITTED', 'ACCEPTED'))
        if new is None:
            return False
        for a in new:
            user, ok = a['user_id'], True
            res = self.check_tasks(a)
            if not res:
                self.mark(pool, a)
                continue
            ratio = sum(r[1] for r in res) / len(res)
            score = f1(sum(r[2] for r in res), sum(r[3] for r in res), sum(r[4] for r in res))
            bad_photos = sum(1 for r in res if r[3] or r[4])   # фото с пропущенной или лишней рамкой при IoU ≥ iou
            if 'max_failed_photos' in pool:   # решение 13.11: ошибка допустима максимум на одном фото
                passed = bad_photos <= pool['max_failed_photos']
            else:
                passed = ratio >= pool.get('min_count_ratio', 0) and score >= pool['min_f1']
            if a['id'] not in self.tries:
                jl(DECISIONS_F, {'time': now(), 'pool': pool['pool_id'], 'type': 'exam', 'assignment': a['id'], 'user': user,
                                 'count_ratio': round(ratio, 3), 'f1': round(score, 3), 'bad_photos': bad_photos, 'photos': len(res),
                                 'passed': passed, 'live': self.live})
            key = f"{pool['skill_id']}:{user}"
            if key not in self.state['graded']:
                value = pool['pass_value'] if passed else pool['fail_value']
                ok = self.write('PUT', 'user-skills', {'skill_id': pool['skill_id'], 'user_id': user, 'value': value},
                                f"экзамен bbox: {'сдал' if passed else 'не сдал'} (фото с ошибкой {bad_photos} из {len(res)}, "
                                f"F1 рамок {score:.2f} при IoU {self.cfg.get('iou', 0.65)}), навык {value}", 'qc_exam_skill')
                if ok and self.live:
                    self.state['graded'][key] = value
            self.mark(pool, a, ok)
        return True

    def do_bbox(self, pool):
        new = self.new_assignments(pool, ('SUBMITTED',))
        if new is None:
            return False
        users = self.state['users'].setdefault(pool['pool_id'], {})   # user → {страница: {honeypots, failed}}
        banned_now, rejected_now = set(), set()
        for a in new:
            user, ok = a['user_id'], True
            key = f"{pool['project_id']}:{user}"
            if a['id'] in rejected_now or user in banned_now or key in self.state['banned']:
                if a['id'] not in rejected_now:   # заблокирован раньше, а страница новая
                    ok = self.write('PATCH', f"assignments/{a['id']}", {'status': 'REJECTED', 'public_comment': COMMENT_FAIL},
                                    'страница заблокированного исполнителя отклонена', 'qc_reject')
                self.mark(pool, a, ok)
                continue
            akey = f"{pool.get('author_skill_id')}:{user}"
            if pool.get('author_skill_id') and akey not in self.state['graded']:   # автор не должен проверять свою работу в review
                if self.write('PUT', 'user-skills', {'skill_id': pool['author_skill_id'], 'user_id': user, 'value': 100},
                              'навык «автор»: сдал страницу обводки', 'qc_author_skill'):
                    if self.live:
                        self.state['graded'][akey] = 100
                else:
                    ok = False
            res = self.check_tasks(a)
            failed = [r for r in res if (r[3] or r[4]) and not r[5]]
            u = users.setdefault(user, {})   # по номерам страниц: повторная обработка ничего не задвоит
            u[a['id']] = {'honeypots': len([r for r in res if not r[5]]), 'failed': [r[0] for r in failed]}
            n_hp, n_failed, n_pages = sum(x['honeypots'] for x in u.values()), sum(len(x['failed']) for x in u.values()), len(u)
            if a['id'] not in self.tries:
                jl(DECISIONS_F, {'time': now(), 'pool': pool['pool_id'], 'type': 'bbox', 'assignment': a['id'], 'user': user,
                                 'honeypots': [r[0] for r in res], 'failed': [r[0] for r in failed], 'user_failed_total': n_failed,
                                 'load_error_on_honeypot': [r[0] for r in res if r[5]], 'user_pages': n_pages,
                                 'live': self.live})
            if failed and pool.get('reject_failed_page', True):
                if self.write('PATCH', f"assignments/{a['id']}", {'status': 'REJECTED', 'public_comment': COMMENT_FAIL},
                              f"ханипот не пройден ({', '.join(r[0] for r in failed)}), страница отклонена", 'qc_reject'):
                    rejected_now.add(a['id'])
                else:
                    ok = False
            if n_failed >= pool['ban_after'] and key not in self.state['banned']:
                banned_now.add(user)
                until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=pool.get('ban_days', 30))
                         ).strftime('%Y-%m-%dT%H:%M:%S')
                if self.write('PUT', 'user-restrictions',
                              {'scope': 'PROJECT', 'project_id': pool['project_id'], 'user_id': user, 'will_expire': until,
                               'private_comment': f"сервис КК: провалено ханипотов {n_failed} из {n_hp}"},
                              f"блокировка в проекте {pool['project_id']}: провалено ханипотов {n_failed} из {n_hp}",
                              'qc_ban'):
                    if self.live:
                        self.state['banned'][key] = now()
                else:
                    ok = False
                others, _ = self.get_all('assignments', {'pool_id': pool['pool_id'], 'user_id': user, 'status': 'SUBMITTED'})
                for other in others or []:
                    if other['id'] not in rejected_now:
                        if self.write('PATCH', f"assignments/{other['id']}", {'status': 'REJECTED', 'public_comment': COMMENT_FAIL},
                                      'страница заблокированного исполнителя отклонена', 'qc_reject'):
                            rejected_now.add(other['id'])
            limit = pool.get('max_pages_per_user')
            lkey = f"{pool['pool_id']}:{user}"
            if limit and n_pages >= limit and lkey not in self.state['limited'] and user not in banned_now:
                if self.write('PUT', 'user-restrictions',
                              {'scope': 'POOL', 'pool_id': pool['pool_id'], 'user_id': user,
                               'private_comment': f'сервис КК: лимит {limit} стр. на человека'},
                              f"лимит страниц: {n_pages} из {limit}, запрет на пул {pool['pool_id']}", 'qc_page_limit'):
                    if self.live:
                        self.state['limited'][lkey] = now()
                else:
                    ok = False
            self.mark(pool, a, ok)
        return True

    def run(self, once):
        while True:
            self.reload_if_changed()
            if STOP_F.exists():
                if once:
                    print(f'{now()} есть {STOP_F.name}, выход', flush=True)
                    return
                self.beat(paused=True)
                time.sleep(self.cfg.get('poll_seconds', 2))
                continue
            good = True
            for pool in self.cfg['pools']:
                good &= (self.do_exam if pool['type'] == 'exam' else self.do_bbox)(pool)
            self.loops += 1
            self.backoff = 0 if good else min(60, max(4, self.backoff * 2))   # API не отвечает: реже, до минуты
            self.beat()
            if once:
                print(f'{now()} один проход сделан', flush=True)
                return
            time.sleep(self.backoff or self.cfg.get('poll_seconds', 2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', required=True)
    ap.add_argument('--live', action='store_true')
    ap.add_argument('--once', action='store_true')
    a = ap.parse_args()
    try:
        Service(a.config, a.live).run(a.once)
    except KeyboardInterrupt:
        print('\nостановлено вручную')


if __name__ == '__main__':
    main()
