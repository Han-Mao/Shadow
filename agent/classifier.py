"""TaskClassifier（V2 §四 / §五）：判断新指令与在跑任务是什么关系。

刻意不做「一次 LLM 定生死」，而是三层融合：

    规则预筛 → 相似度 → LLM 判定 → 融合

理由是单靠 LLM 判关系极不稳定：它容易被措辞带偏，也解释不了「为什么这么判」。
规则层提供可解释的强证据，相似度层提供客观相关性，LLM 只在两者之上做加权。
"""
from __future__ import annotations

import logging
import math
import re
from typing import Callable, Iterable, Sequence

from models.task import Task
from models.task_relation import TaskRelation, TaskRelationResult

logger = logging.getLogger(__name__)

# 句首出现这些词，几乎可以确定用户是在给当前任务追加子步骤
SUBTASK_LEADERS = ("先", "先帮我", "先给我", "其中", "顺便", "然后", "接着", "再帮我", "把这个")
# 出现在句中时只是弱信号（例如「打开设置，先看看网络」）
SUBTASK_INLINE = ("其中", "顺便", "把这个", "在此基础上")

INTERRUPT_MARKERS = ("马上", "立刻", "立即", "紧急", "现在就要", "赶紧", "停一下", "打断一下", "别管了")
SUPER_TASK_MARKERS = ("改成", "换成", "不要了", "重来", "重新来", "取消刚才", "我说的不是")

# 融合权重
LLM_WEIGHT = 0.6
RULE_WEIGHT = 0.3
SIMILARITY_WEIGHT = 0.1

RULE_LEADER_SCORE = 0.75
RULE_INLINE_SCORE = 0.45
RULE_MARKER_SCORE = 0.8

# 低于这个分就不采信规则结论，退化为「无关任务」
RULE_MIN_SCORE = 0.4
# 与已有任务相似到这个程度就判定重复，不再重复执行
DUPLICATE_SIMILARITY = 0.85

# 这些关系要求两句话确实相关：subtask 是「当前任务的一部分」，
# 但「先帮我打开微信发消息」对「淘宝搜索运动鞋」也会因为「先」命中规则——
# 两者毫无交集，这时必须否决，否则会把无关任务塞进当前计划。
RELATION_NEEDS_AFFINITY = frozenset({TaskRelation.SUBTASK, TaskRelation.DUPLICATE})

# 相关性下限（V2.2 §八；V2.7 P1-3 重新定位）。
#
# 以前它是**唯一的否决器**：`relevance = max(字面, 语义)` 低于它就一票否决。审核指出
# 两个方向都有问题：
#   - 字面相似度不该单独当否决器——「帮我规划去上海的旅行」+「先帮我订酒店」几乎零
#     重叠，却是真的依赖关系；
#   - `max` 取最大值又偏激进——「给妈妈发微信」+「给爸爸发微信」字面很高，却是两件事。
# 所以它现在的含义收窄为：**既没有明确语言标记、也没有共享实词时**，相关性要有多高
# 才承认「这和当前任务是同一件事」。
AFFINITY_FLOOR = 0.35

# LLM 判成「需要相关性」的那两类关系（SUBTASK / DUPLICATE）时，置信度到多少才算
# **明确表达了依赖**——用来豁免上面的相关性否决（V2.7 P1-4）。
LLM_DEPENDENT_CONFIDENCE = 0.6

LLMJudge = Callable[[str, str], TaskRelationResult]

Embedder = Callable[[str], Sequence[float]]
"""把一句话变成向量。注入后启用语义相似度；不注入就纯 Jaccard（离线可跑，零依赖）。"""


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """余弦相似度。维度不一致或零向量时返回 0，不抛异常——相似度只是信号之一。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_l = math.sqrt(sum(a * a for a in left))
    norm_r = math.sqrt(sum(b * b for b in right))
    if norm_l == 0 or norm_r == 0:
        return 0.0
    return dot / (norm_l * norm_r)


def _tokens(text: str) -> set[str]:
    """中英混合分词：英文按单词，中文按二元组（免分词器依赖，对短指令够用）。"""
    lowered = (text or "").lower()
    words = set(re.findall(r"[a-z0-9]+", lowered))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    bigrams = {cjk[i : i + 2] for i in range(len(cjk) - 1)}
    return words | bigrams


def instruction_similarity(left: str, right: str) -> float:
    """Jaccard 相似度。0 表示毫无重叠。"""
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def shared_terms(left: str, right: str) -> set[str]:
    """两句话共享的**实词**（V2.7 P1-3）。

    这是「共享对象」的最粗糙证据：长度 ≥ 2 的 token 交集（中文按二元组天然 ≥ 2 字，
    英文单词要求两个字母以上，滤掉 a / is 这类噪声）。

    审核把它的必要性说得很准：字面相似度适合当**相关性证据**，不适合单独当**关系判定器**。
    真实的依赖关系经常几乎零字面重叠（「帮我规划一次去上海的旅行」+「先帮我订酒店」），
    而字面高度相似又可能是两件独立的事（「给妈妈发微信」+「给爸爸发微信」）。
    """
    return {token for token in (_tokens(left) & _tokens(right)) if len(token) >= 2}


# ---- 实体冲突（V3.1 P1-7）----
#
# 审核意见把这一层为什么必须存在讲得很准：
#
#     「给妈妈发微信」+「给爸爸发微信」
#         ↓ 字面相似度很高、语义相似度更高（几乎同一句话）
#         ↓ 于是被判成 SUBTASK / DUPLICATE
#         ↓ 合并或者当成重复而跳过 —— 但这是**两条要发的消息**
#
# 这类任务之间最要紧的差异不是语义，而是**主体 / 对象**。相似度天然表达不了它：
# 越相似的句子，恰恰越可能是「同一件事换了个对象」。所以要单独看「有没有互斥实体」。
#
# 表按**同维度**分组：同一组内出现不同成员 = 冲突（妈妈 vs 爸爸、淘宝 vs 京东）；
# 两句话都提同一个成员 = 不冲突。分组刻意收窄——把「淘宝」和「微信」放进同一个
# 大组会让「在淘宝下单」+「然后用微信支付」被判成互斥，那是真的递进关系。
ENTITY_GROUPS: tuple[frozenset[str], ...] = (
    # 联系人 / 亲属：不同收件人就是不同的事
    frozenset({
        "妈妈", "母亲", "妈", "爸爸", "父亲", "爸", "老婆", "妻子", "老公", "丈夫",
        "姐姐", "妹妹", "哥哥", "弟弟", "爷爷", "奶奶", "外公", "外婆", "外公",
        "儿子", "女儿", "女朋友", "男朋友", "女友", "男友", "老师", "老板", "客户",
    }),
    # 电商 / 购物平台：不同商户就是不同的订单
    frozenset({
        "淘宝", "天猫", "京东", "拼多多", "唯品会", "苏宁", "亚马逊", "得物", "闲鱼",
        "taobao", "tmall", "jingdong", "pinduoduo", "vipshop", "amazon",
    }),
    # 通讯渠道：发微信 ≠ 发短信 ≠ 打电话
    frozenset({
        "微信", "短信", "电话", "邮件", "邮箱", "qq", "钉钉", "飞书", "whatsapp",
        "wechat", "sms", "telegram",
    }),
    # 支付渠道：换一个渠道就是另一笔支付
    frozenset({
        "支付宝", "云闪付", "银行卡", "信用卡", "花呗", "余额", "零钱", "alipay",
    }),
    # 银行：换一张卡就是另一个账户
    frozenset({
        "招商", "工商", "建设银行", "农业银行", "中国银行", "交通银行", "邮储",
        "浦发", "中信", "民生", "兴业", "平安银行",
    }),
)


def conflicting_entities(left: str, right: str) -> frozenset[str]:
    """两句话里「同一维度、但取值不同」的实体（V3.1 P1-7）。

    返回冲突到的实体集合；没有冲突返回空集。「两句话都提到同一个成员」不算冲突
    ——那反而说明指的是同一个对象。
    """
    text_left = (left or "").lower()
    text_right = (right or "").lower()
    conflicts: set[str] = set()
    for group in ENTITY_GROUPS:
        hits_left = {entity for entity in group if entity in text_left}
        hits_right = {entity for entity in group if entity in text_right}
        if hits_left and hits_right and not (hits_left & hits_right):
            conflicts |= hits_left | hits_right
    return frozenset(conflicts)


class TaskClassifier:
    def __init__(
        self,
        llm_judge: LLMJudge | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        # 无 LLM 时纯规则运行：本地开发和 CI 都不需要 API Key
        self._llm_judge = llm_judge
        # 语义相似度是**可选增强**（V2.1 §八）：Jaccard 只认字面重叠，
        # 「订酒店」和「找住宿」这种同义改写它判为 0；有 embedder 时能补上。
        self._embedder = embedder

    def _semantic_similarity(self, text: str, other: str) -> float:
        if self._embedder is None:
            return 0.0
        try:
            return max(0.0, cosine_similarity(self._embedder(text), self._embedder(other)))
        except Exception as exc:  # noqa: BLE001 - 向量化失败必须降级，不能拖垮注入流程
            logger.warning("语义相似度计算失败，回退到 Jaccard: %s", exc)
            return 0.0

    def classify(
        self,
        instruction: str,
        *,
        current: Task | None = None,
        candidates: Iterable[Task] = (),
    ) -> TaskRelationResult:
        text = (instruction or "").strip()
        if not text:
            return TaskRelationResult(reason="指令为空")

        signals: dict[str, float] = {}

        # ---- 1. 与已有任务重复？ ----
        for task in candidates:
            similarity = instruction_similarity(text, task.instruction)
            if similarity < DUPLICATE_SIMILARITY:
                continue
            # V3.1 P1-7：字面高度相似、但对象互斥 → 是两件事，不是重复。
            # 这一条比「判错关系」更要紧：判成 DUPLICATE 的后果是**静默不执行**
            # （「与已有任务重复，不再重复执行」）。「给妈妈发微信」和「给爸爸发微信」
            # 的 Jaccard 并不低，纯比字面会把第二条真实指令当成重复丢掉。
            conflicts = conflicting_entities(text, task.instruction)
            if conflicts:
                signals["duplicate_entity_conflict"] = float(len(conflicts))
                continue
            signals["duplicate_similarity"] = round(similarity, 3)
            return TaskRelationResult(
                relation=TaskRelation.DUPLICATE,
                confidence=min(0.95, 0.5 + similarity / 2),
                reason=f"与任务 {task.id} 的指令高度相似（{similarity:.2f}），不重复执行",
                affected_task_id=task.id,
                signals=signals,
            )

        # ---- 2. 规则层 ----
        rule_scores = self._rule_scores(text)
        signals.update({f"rule.{k.value}": round(v, 3) for k, v in rule_scores.items()})
        best_relation, best_rule_score = max(rule_scores.items(), key=lambda item: item[1])

        # ---- 3. 相似度层：字面 + 语义取较大者 ----
        similarity = instruction_similarity(text, current.instruction) if current else 0.0
        signals["similarity"] = round(similarity, 3)

        semantic = self._semantic_similarity(text, current.instruction) if current else 0.0
        if self._embedder is not None:
            signals["semantic_similarity"] = round(semantic, 3)

        # 相关性取两者较大值：Jaccard 认字面重叠，「订酒店」/「找住宿」这类同义改写它判 0；
        # 语义相似度能补上，但可能把泛泛相关的句子也拉高。任一有证据就算相关，
        # 两者都证据不足才判无关——所以取 max 而不是取平均。
        relevance = max(similarity, semantic)
        signals["relevance"] = round(relevance, 3)

        # ---- 4. LLM 层 ----
        llm_result: TaskRelationResult | None = None
        if self._llm_judge is not None and current is not None:
            try:
                llm_result = self._llm_judge(text, current.instruction)
                signals[f"llm.{llm_result.relation.value}"] = round(llm_result.confidence, 3)
            except Exception as exc:  # noqa: BLE001 - LLM 判定失败必须降级到规则层
                logger.warning("LLM 关系判定失败，回退到规则层: %s", exc)
                llm_result = None

        # ---- 5. 融合 ----
        if llm_result is not None:
            relation = llm_result.relation
            confidence = (
                LLM_WEIGHT * llm_result.confidence
                + RULE_WEIGHT * best_rule_score
                + SIMILARITY_WEIGHT * relevance
            )
            reason = (
                f"LLM 判定 {llm_result.relation.value}（{llm_result.confidence:.2f}）；"
                f"规则最高 {best_relation.value}={best_rule_score:.2f}"
            )
        elif best_rule_score < RULE_MIN_SCORE:
            return TaskRelationResult(
                relation=TaskRelation.UNRELATED,
                confidence=round(1 - best_rule_score, 3),
                reason="规则层无强信号，判定为独立新任务",
                signals=signals,
            )
        else:
            # 无 LLM 时把「规则 + 相似度」重新归一化到 1.0 权重，保持阈值含义一致
            total_weight = RULE_WEIGHT + SIMILARITY_WEIGHT
            relation = best_relation
            confidence = (
                RULE_WEIGHT * best_rule_score + SIMILARITY_WEIGHT * relevance
            ) / total_weight
            reason = (
                f"规则命中 {best_relation.value}（{best_rule_score:.2f}），"
                f"相关性 {relevance:.2f}（字面 {similarity:.2f}"
                + (f" / 语义 {semantic:.2f}" if self._embedder is not None else "")
                + "）"
            )

        # ---- 6. 相关性 + 共享对象 + 明确依赖（V2.7 P1-3 / P1-4）----
        #
        # 审核把两件事分得很准，这里一起处理：
        #   P1-4「相似度适合当相关性证据，不适合单独当否决器」
        #        → 有明确语言标记（「先…」「顺便…」）或 LLM 明确判依赖时，豁免否决；
        #   P1-3「max 取最大值偏激进」
        #        → 除此之外还要看「有没有共享实词」，防止字面撞车被误并进来。
        shared = shared_terms(text, current.instruction) if current is not None else set()
        signals["shared_terms"] = float(len(shared))

        if relation in RELATION_NEEDS_AFFINITY and current is not None:
            # V3.1 P1-7：**实体冲突优先于一切相关性证据**。
            #
            # 这一条必须放在「共享实词 / 明确语言标记」豁免之前，否则挡不住真正的坑：
            # 「给妈妈发微信」+「先给爸爸发微信」里「先」命中 SUBTASK_LEADERS（0.75），
            # 两句又共享「发微 / 微信」（shared_terms 非空），两条豁免全中 → 判 SUBTASK
            # → 并进当前计划。可它们要发的是**两条不同的消息**。
            #
            # 相似度表达不了这件事：越相似的两句话，越可能只是「同一件事换了个对象」。
            conflicts = conflicting_entities(text, current.instruction)
            if conflicts:
                signals["entity_conflict"] = float(len(conflicts))
                return TaskRelationResult(
                    relation=TaskRelation.UNRELATED,
                    confidence=round(min(0.85, 0.5 + best_rule_score * 0.3), 3),
                    reason=(
                        f"倾向判为 {relation.value}，但两句话指向互斥对象"
                        f"（{'/'.join(sorted(conflicts))}），是两件独立的事"
                    ),
                    signals=signals,
                )

            llm_says_dependent = (
                llm_result is not None
                and llm_result.relation in RELATION_NEEDS_AFFINITY
                and llm_result.confidence >= LLM_DEPENDENT_CONFIDENCE
            )
            explicit_dependency = best_rule_score >= RULE_INLINE_SCORE or llm_says_dependent
            if relevance < AFFINITY_FLOOR and not explicit_dependency and not shared:
                signals["affinity_veto"] = round(relevance, 3)
                return TaskRelationResult(
                    relation=TaskRelation.UNRELATED,
                    confidence=round(min(0.85, 0.5 + best_rule_score * 0.3), 3),
                    reason=(
                        f"倾向判为 {relation.value}，但既没有明确的语言标记，"
                        f"也没有共享对象（相关性 {relevance:.2f}），按独立任务处理"
                    ),
                    signals=signals,
                )

        return TaskRelationResult(
            relation=relation,
            confidence=round(min(0.9, confidence), 3),
            reason=reason,
            affected_task_id=current.id if current else None,
            signals=signals,
        )

    # ---- 规则打分 ----

    @staticmethod
    def _rule_scores(instruction: str) -> dict[TaskRelation, float]:
        text = instruction.strip()
        scores = {
            TaskRelation.SUBTASK: 0.0,
            TaskRelation.INTERRUPT: 0.0,
            TaskRelation.SUPER_TASK: 0.0,
        }

        for leader in SUBTASK_LEADERS:
            if text.startswith(leader):
                scores[TaskRelation.SUBTASK] = max(scores[TaskRelation.SUBTASK], RULE_LEADER_SCORE)
                break
        else:
            for marker in SUBTASK_INLINE:
                if marker in text:
                    scores[TaskRelation.SUBTASK] = max(scores[TaskRelation.SUBTASK], RULE_INLINE_SCORE)
                    break

        for marker in INTERRUPT_MARKERS:
            if marker in text:
                scores[TaskRelation.INTERRUPT] = max(scores[TaskRelation.INTERRUPT], RULE_MARKER_SCORE)
                break

        for marker in SUPER_TASK_MARKERS:
            if marker in text:
                scores[TaskRelation.SUPER_TASK] = max(scores[TaskRelation.SUPER_TASK], RULE_MARKER_SCORE)
                break

        return scores
