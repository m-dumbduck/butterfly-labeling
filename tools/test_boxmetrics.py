#!/usr/bin/env python3
"""Тесты tools/boxmetrics.py и tools/check_reference_answers.py на известных «исполнителях».

Запуск из папки project: python3 -I tools/test_boxmetrics.py
"""
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / 'tools'))
from boxmetrics import counts, f1, iou, match  # noqa: E402

ok = True


def check(cond, what):
    global ok
    print(('OK   ' if cond else 'FAIL ') + what)
    ok &= bool(cond)


B = lambda l, t, w, h: {'left': l, 'top': t, 'width': w, 'height': h}
check(abs(iou(B(0, 0, .5, .5), B(0, 0, .5, .5)) - 1) < 1e-9, 'IoU одинаковых рамок = 1')
check(abs(iou(B(0, 0, .2, .2), B(.1, 0, .2, .2)) - 1 / 3) < 1e-9, 'IoU половинного сдвига = 1/3')
check(iou(B(0, 0, .1, .1), B(.5, .5, .1, .1)) == 0, 'IoU непересекающихся = 0')
# жадное сопоставление тут даёт 1 пару, максимальное паросочетание 2
# gt0 подходит к pred0 (IoU 0.82) и pred1 (0.6), gt1 только к pred0 (0.6): жадно gt0→pred0 и gt1 без пары
gt = [B(.10, 0, .4, .4), B(.24, 0, .4, .4)]
pred = [B(.14, 0, .4, .4), B(0, 0, .4, .4)]
check(len(match(gt, pred, 0.5)) == 2, 'максимальное паросочетание находит 2 пары там, где жадное нашло бы 1')
check(counts([], [], .5) == (0, 0, 0) and f1(0, 0, 0) == 1.0, 'пустой эталон и пустой ответ: ошибок нет')
check(counts([], [B(0, 0, .1, .1)], .5) == (0, 1, 0), 'рамка на пустом фото = FP')
check(counts([B(0, 0, .1, .1)], [], .5) == (0, 0, 1), 'пропуск = FN')

# «исполнители» на 7 эталонных фото экзамена из контрольного набора (эталон = рамки Олега)
ref_rows = json.load(open(HERE / 'control' / 'control_reference_v1.json', encoding='utf-8'))['rows']
exam = [r for r in ref_rows if r['role'] == 'exam' and r['count'] != '0']
reference = {r['photo_id']: {'count': r['count'], 'boxes': r['boxes']} for r in exam}
rng = random.Random(1)


def assignment(user, boxes_of, count_of):
    return {'id': 'a_' + user, 'user_id': user, 'status': 'SUBMITTED',
            'tasks': [{'input_values': {'photo_id': r['photo_id']}} for r in exam],
            'solutions': [{'output_values': {'boxes': boxes_of(r), 'count': count_of(r)}} for r in exam]}


def rand_box():
    w, h = rng.uniform(.05, .3), rng.uniform(.05, .3)
    return B(rng.uniform(0, 1 - w), rng.uniform(0, 1 - h), w, h)


items = [
    assignment('maksim', lambda r: r['maksim_boxes'], lambda r: r['count']),
    assignment('gpt', lambda r: r['gpt_boxes'], lambda r: r['count']),
    assignment('cheater_random_boxes', lambda r: [rand_box() for _ in r['boxes']], lambda r: r['count']),
    assignment('lazy_zero', lambda r: [], lambda r: '0'),
]
with tempfile.TemporaryDirectory() as d:
    (Path(d) / 'a.json').write_text(json.dumps({'items': items}))
    (Path(d) / 'ref.json').write_text(json.dumps(reference))
    r = subprocess.run([sys.executable, '-I', str(HERE / 'tools' / 'check_reference_answers.py'), '--assignments',
                        str(Path(d) / 'a.json'), '--reference', str(Path(d) / 'ref.json'), '--out', str(Path(d) / 'o.tsv')],
                       capture_output=True, text=True)
    print(r.stdout.strip())
    res = {l.split('\t')[0]: l.split('\t') for l in (Path(d) / 'o.tsv').read_text().splitlines()[1:]}
    check(res['maksim'][-2] == 'True' and res['gpt'][-2] == 'True', 'Максим и GPT проходят (их рамки близки к эталону)')
    check(res['cheater_random_boxes'][-2] == 'False', 'халтурщик с верным числом и случайными рамками не проходит')
    check(res['lazy_zero'][-2] == 'False', '«везде 0» не проходит')
sys.exit(0 if ok else 1)
