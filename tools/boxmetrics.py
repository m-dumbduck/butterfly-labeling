"""Сравнение рамок: IoU и object F1 как в Контесте.

Рамка: {"left", "top", "width", "height"} в долях 0..1.
Сопоставление внутри фото: максимальное число пар «эталон — ответ» с IoU ≥ порога, каждая рамка не больше чем в одной паре
(максимальное паросочетание, как описано в условии Контеста). TP/FP/FN суммируются по набору, F1 = 2TP / (2TP + FP + FN).
"""


def iou(a, b):
    ax2, ay2 = a['left'] + a['width'], a['top'] + a['height']
    bx2, by2 = b['left'] + b['width'], b['top'] + b['height']
    iw = max(0.0, min(ax2, bx2) - max(a['left'], b['left']))
    ih = max(0.0, min(ay2, by2) - max(a['top'], b['top']))
    inter = iw * ih
    union = a['width'] * a['height'] + b['width'] * b['height'] - inter
    return inter / union if union > 0 else 0.0


def match(gt, pred, thr=0.5):
    """Максимальное паросочетание по рёбрам IoU ≥ thr. Возвращает список пар (i_gt, j_pred, iou)."""
    adj = [[j for j, p in enumerate(pred) if iou(g, p) >= thr] for g in gt]
    owner = {}   # j_pred -> i_gt

    def augment(i, seen):
        for j in adj[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in owner or augment(owner[j], seen):
                owner[j] = i
                return True
        return False

    for i in range(len(gt)):
        augment(i, set())
    return sorted((i, j, iou(gt[i], pred[j])) for j, i in owner.items())


def counts(gt, pred, thr=0.5):
    """TP, FP, FN для одного фото."""
    tp = len(match(gt, pred, thr))
    return tp, len(pred) - tp, len(gt) - tp


def f1(tp, fp, fn):
    d = 2 * tp + fp + fn
    return 2 * tp / d if d else 1.0   # пустой эталон и пустой ответ: ошибок нет
