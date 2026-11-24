"""内置统一筛级与级配计算（纯函数）。"""

# 内置统一筛级：各档上限（mm），由细到粗排列。
# 第 i 档 = 粒径 ∈ (上限[i-1], 上限[i]] 的颗粒，第 0 档 = ≤ 0.063 mm。
GRADES_MM = [0.063, 0.125, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0]
N_GRADES = len(GRADES_MM)


def cumulative_from_fine(weights):
    """由细到粗的累计干重（克）。"""
    out = []
    run = 0
    for w in weights:
        run += w
        out.append(run)
    return out


def d50_mm(weights):
    """从细到粗累计首次达到总干重一半的筛级上限（mm）。

    总干重为零时返回 None（无样）。
    """
    total = sum(weights)
    if total <= 0:
        return None
    run = 0
    for w, upper in zip(weights, GRADES_MM):
        run += w
        if 2 * run >= total:  # 累计首次达到总干重的一半（整数比较避免浮点误差）
            return upper
    return None  # 不可达：累计末值等于总干重
