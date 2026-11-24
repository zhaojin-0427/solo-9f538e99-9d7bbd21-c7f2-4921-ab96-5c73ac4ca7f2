"""内置统一筛级、合并档解析与级配计算（纯函数）。

内置统一筛级共 11 档，各档上限（mm）由细到粗排列：
第 i 档 = 粒径 ∈ (上限[i-1], 上限[i]] 的颗粒，第 0 档 = ≤ 0.063 mm。

换筛具后，部分日样只能以"相邻内置档合并"的粒度上报。合并档用其档上限
（必须取某个内置档上限，最粗一档上限为 64 mm）和该档非负整数克重表示：
- 合并档 k（上限为内置档 j 的上限）覆盖内置档 (prev_j, j]，即 j−(跨度)+1 … j；
- 克重记在其上限所属的内置档 j 上（j 列），其余覆盖档记 0；
- 非零合并档若覆盖多个内置档，就"跨过"了这些内置档之间的内部档界，
  用一个 10 位掩码 mask 记录（位 b 对应档界 GRADES_MM[b]，b=0..9）。

档界 GRADES_MM[b]（b=0..9，即除最粗 64 mm 外的 10 个上限）是内置档 b 与
b+1 之间的分界。合并档覆盖内置档 (lo, hi] 时，跨过档界 lo .. hi−1。
"""

# 内置统一筛级：各档上限（mm），由细到粗排列。
GRADES_MM = [0.063, 0.125, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0]
N_GRADES = len(GRADES_MM)
# 内部档界：相邻内置档之间的分界，共 N_GRADES−1 个（不含最粗上限 64 mm）。
N_BOUNDARIES = N_GRADES - 1
BOUNDARIES_MM = GRADES_MM[:N_BOUNDARIES]
COARSEST_MM = GRADES_MM[-1]


class BinParseError(ValueError):
    """合并档请求不合法。"""


def boundary_mask_for_span(lo, hi):
    """覆盖内置档 (lo, hi]（即 lo+1 … hi）的非零档跨过的档界掩码。

    档界 b（b=0..N_BOUNDARIES−1，上限 GRADES_MM[b]）是内置档 b−1 与 b
    之间的分界。一个合并档只在覆盖多个内置档（hi > lo+1）时才跨界，
    跨过的是其内部档界 lo+1, …, hi−1；档界 hi 是该档自身的上限，不算跨过。
    例如 lo=2、hi=5（覆盖档 3..5）跨过档界 3、4（上限 0.5、1.0 mm）；
    lo=−1、hi=2（覆盖档 0..2）跨过档界 0、1（上限 0.063、0.125 mm）。
    """
    mask = 0
    for b in range(lo + 1, hi):
        if 0 <= b < N_BOUNDARIES:
            mask |= 1 << b
    return mask


def parse_merged_bins(bins):
    """把合并档列表解析为 (11 档克重, 跨界掩码)。

    bins 为 (upper_mm, grams) 序列，upper_mm 必须严格升序且取自内置档上限，
    最粗一档上限须为 64 mm；grams 为非负整数。第一个档隐式覆盖最细的内置档
    （上限 0.063 mm）起。

    返回 (weights, mask)：
    - weights 长度 N_GRADES，克重记在各合并档上限所属内置档列上；
    - mask 为非零合并档跨过的内部档界掩码（零重量档可拆分，不计跨界）。
    """
    if not bins:
        raise BinParseError("合并档至少需要 1 档")
    weights = [0] * N_GRADES
    mask = 0
    prev_j = -1
    for upper_mm, grams in bins:
        try:
            j = GRADES_MM.index(upper_mm)
        except ValueError:
            raise BinParseError(f"档上限 {upper_mm} 不是内置筛级上限") from None
        if j <= prev_j:
            raise BinParseError("合并档上限须按粒径严格升序排列")
        if not isinstance(grams, int) or isinstance(grams, bool) or grams < 0:
            raise BinParseError("各档克重须为非负整数")
        weights[j] = grams
        if grams > 0:
            mask |= boundary_mask_for_span(prev_j, j)
        prev_j = j
    if prev_j != N_GRADES - 1:
        raise BinParseError(f"最粗一档上限须为 {COARSEST_MM} mm")
    return weights, mask


def crossed_boundaries(mask):
    """由跨界掩码返回被跨过的档界上限（mm），按升序排列。"""
    return [GRADES_MM[b] for b in range(N_BOUNDARIES) if mask & (1 << b)]


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

    总干重为零时返回 None（无样）。仅在所有档界都确定（无跨界）时才有意义。
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
