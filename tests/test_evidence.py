"""多级证据（V2.2 §六）：把「页面变了吗」从 label 集合升级成四条独立证据。

被锁定的三个反例（来自审核）：

    toast 弹出但 label 集合没变          → 旧实现漏判「成功」
    页面内部数据变了、label 集合没变      → 旧实现漏判「变化」
    无关 Dialog 弹出、label 集合变了      → 旧实现误判「成功」

新实现用结构指纹做主判据，并把目标元素自身的状态（消失 / 文本变化）单列一层。
"""
from __future__ import annotations

from agent import evidence
from agent.evidence import EvidenceLevel, screen_delta
from agent.verifier import ui_tree_signal
from models.action import Action, ActionEffectStatus, ActionType, Point
from models.state import Observation
from vision.target import TargetState, resolve_target, target_state

SUBMIT = (
    '<hierarchy>'
    '<node class="android.widget.Button" text="提交" bounds="[0,0][100,50]" '
    'clickable="true" resource-id="app:id/submit"/>'
    "</hierarchy>"
)
SUBMITTED = (
    '<hierarchy>'
    '<node class="android.widget.Button" text="已提交" bounds="[0,0][100,50]" '
    'clickable="true" resource-id="app:id/submit"/>'
    "</hierarchy>"
)


def obs(ui_tree: str, *, package: str = "com.demo", activity: str = ".Main") -> Observation:
    return Observation(
        step=1,
        screenshot_path="/tmp/a.png",
        package=package,
        activity=activity,
        ui_tree=ui_tree,
        screen_size=(1000, 2000),
    )


def tap() -> Action:
    return Action(type=ActionType.TAP, target=Point(x=50, y=25))


# ---------------------------------------------------------------- 结构层


def test_text_only_change_is_detected():
    """「提交」→「已提交」：label 集合看起来都是「一个可点击元素」，
    但内容变了——指纹包含 text，所以能判出结构变化。"""
    delta = screen_delta(obs(SUBMIT), obs(SUBMITTED), tap())

    assert delta.known
    assert delta.structural_changed
    assert not delta.navigation_changed
    assert delta.strongest_layer is EvidenceLevel.L3_STRUCTURE


def test_identical_trees_are_not_treated_as_changed():
    """同页重绘不能让 L3 永远判「变了」——指纹刻意不含 bounds，就是为此。"""
    same_but_jittered = (
        '<hierarchy>'
        '<node class="android.widget.Button" text="提交" bounds="[0,7][100,58]" '
        'clickable="true" resource-id="app:id/submit"/>'
        "</hierarchy>"
    )

    delta = screen_delta(obs(SUBMIT), obs(same_but_jittered), tap())

    assert not delta.structural_changed, "bounds 抖动不该被算成结构变化"


# ---------------------------------------------------------------- 导航层


def test_activity_change_is_the_strongest_local_evidence():
    delta = screen_delta(obs(SUBMIT, activity=".Main"), obs(SUBMIT, activity=".Home"), tap())

    assert delta.navigation_changed
    assert delta.strongest_layer is EvidenceLevel.L2_NAVIGATION


def test_unparseable_tree_reports_unknown_not_unchanged():
    """树不可比 → unknown。不能把它当成「没变化」——
    那会让「拿不到证据」被误读成「反证」。"""
    delta = screen_delta(obs("<hierarchy><node"), obs(SUBMIT), tap())

    assert not delta.known
    assert not delta.changed
    assert delta.strongest_layer is EvidenceLevel.NONE
    assert ui_tree_signal(obs("<hierarchy><node"), obs(SUBMIT)) == "unknown"


# ---------------------------------------------------------------- 目标层


def test_target_element_state_tracks_disappearance_and_change():
    action = tap()

    assert target_state(action, SUBMIT, SUBMITTED) is TargetState.CHANGED
    assert target_state(action, SUBMIT, SUBMIT) is TargetState.UNCHANGED
    assert target_state(action, SUBMIT, "<hierarchy/>") is TargetState.GONE


def test_target_state_is_skipped_without_a_target():
    """LAUNCH / WAIT 这类动作没有目标元素，不该假装能判目标状态。"""
    action = Action(type=ActionType.WAIT, value="500")

    assert target_state(action, SUBMIT, SUBMITTED) is TargetState.UNKNOWN


def test_resolve_target_by_description():
    action = Action(type=ActionType.TAP, target="提交")

    resolved = resolve_target(action, SUBMIT, (1000, 2000))

    assert resolved.found
    assert resolved.label == "提交"
    assert resolved.resource_id == "app:id/submit"


def test_positive_target_evidence_is_recognized():
    """目标元素消失/变化本身就是「动作生效」的正向证据。"""
    delta = screen_delta(obs(SUBMIT), obs(SUBMITTED), tap())

    assert delta.target.is_positive_evidence


# ---------------------------------------------------------------- V2.2 §六：危险动作的强证据


def test_success_marker_matches_final_state_text_only():
    """成功标志必须写得保守：只认终态文案，绝不把「支付失败」当成功。"""
    assert evidence.success_evidence(SUBMITTED) == "已提交"
    assert evidence.success_evidence(SUBMIT) == "", "「提交」还不是终态"
    assert evidence.success_evidence(None) == ""
    assert evidence.success_evidence("<hierarchy><node") == "", "树坏了不算证据"
    assert evidence.success_evidence(
        '<hierarchy><node text="支付失败" bounds="[0,0][10,10]"/></hierarchy>'
    ) == ""


def test_dangerous_reconcile_requires_an_explicit_success_marker():
    """「跳到了支付 Activity」不等于「付款成功」（V2.2 §六）。

    旧逻辑：activity 变了 → CONTINUE。但那只说明**进入了支付流程**。
    危险动作现在只看页面上有没有明确的成功标志，没有就转人工。
    """
    from agent.reconciliation import ReconcileAction, reconcile
    from models.action import Action, ActionRisk, ActionType, Point
    from models.checkpoint import Checkpoint

    pay = Action(
        type=ActionType.TAP,
        target=Point(x=10, y=20),
        value="确认付款",
        risk=ActionRisk.DANGEROUS,
    )
    assert pay.resolved_risk() is ActionRisk.DANGEROUS, "前置条件：它确实被判成危险动作"

    cart = obs(
        '<hierarchy><node class="android.widget.Button" text="确认付款" bounds="[0,0][100,50]"/>'
        "</hierarchy>",
        activity=".CartActivity",
    )
    checkpoint = Checkpoint.capture(
        task_id="t1",
        step=1,
        step_states={},
        observation=cart,
        action_effect=ActionEffectStatus.EFFECT_UNKNOWN,
        last_action=pay,
    )

    # 情况 A：跳到了支付页，但页面上没有任何成功标志 → 不能判定成功
    pay_page = obs(
        '<hierarchy><node class="android.widget.TextView" text="请输入支付密码" '
        'bounds="[0,0][100,50]"/></hierarchy>',
        activity=".PayActivity",
    )
    verdict = reconcile(checkpoint, pay_page, online=True)

    assert verdict.action is ReconcileAction.ASK_HUMAN
    assert "进入" in verdict.reason or "跳转" in verdict.reason

    # 情况 B：页面出现明确成功标志 → 才算生效
    paid = obs(
        '<hierarchy><node class="android.widget.TextView" text="支付成功" '
        'bounds="[0,0][100,50]"/></hierarchy>',
        activity=".PayActivity",
    )
    verdict = reconcile(checkpoint, paid, online=True)

    assert verdict.action is ReconcileAction.CONTINUE
    assert verdict.layer == "l5_success_marker"


def test_normal_action_still_accepts_page_change():
    """普通动作不受这条收紧影响：页面跳转仍然是「已生效」的合理证据。"""
    from agent.reconciliation import ReconcileAction, reconcile
    from models.action import Action, ActionType, Point
    from models.checkpoint import Checkpoint

    tap_action = Action(type=ActionType.TAP, target=Point(x=10, y=20))
    before = obs(SUBMIT, activity=".ListActivity")
    checkpoint = Checkpoint.capture(
        task_id="t1",
        step=1,
        step_states={},
        observation=before,
        action_effect=ActionEffectStatus.EFFECT_UNKNOWN,
        last_action=tap_action,
    )

    verdict = reconcile(checkpoint, obs(SUBMITTED, activity=".DetailActivity"), online=True)

    assert verdict.action is ReconcileAction.CONTINUE
