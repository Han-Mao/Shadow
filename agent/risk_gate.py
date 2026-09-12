"""Action Risk Gate（V2.1 §十 / §十一）：所有改变设备的 Action 必经的单一风险门禁。

之前 Runtime 在 Executor 前做 DANGEROUS 门禁，但 API 的 `/actions` 直控端点绕过了它——
直接 `executor.execute` 之后才把 risk 一起返回，等于任何外部调用都能静默执行危险动作。

现在 Runtime 与 API 共用 `ActionRiskGate.assess()`，保证
「任何会改变设备的 Action 都先过 RiskPolicy，再谈执行」（V2.1 §十 的统一链路）。
"""
from __future__ import annotations

from models.action import Action, ActionRisk


class ActionRiskGate:
    """统一的风险评估入口。"""

    @staticmethod
    def assess(action: Action) -> ActionRisk:
        """返回动作的有效风险（effective_risk = max(policy_risk, model_risk)）。

        模型在 action.risk 上的声明只是「建议」，不能把危险动作降为 safe。
        """
        return action.resolved_risk()

    @staticmethod
    def requires_confirmation(action: Action) -> bool:
        """危险动作必须经人工确认，不能静默执行。"""
        return ActionRiskGate.assess(action) is ActionRisk.DANGEROUS
