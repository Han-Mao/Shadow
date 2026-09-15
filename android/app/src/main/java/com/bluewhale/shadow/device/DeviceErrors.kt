package com.bluewhale.shadow.device

/**
 * 设备层抛出的两类失败（V3.3 §3）。
 *
 * 分成两个类型而不是都用 `IllegalStateException`，是因为它们**在核心侧的处置完全不同**：
 *
 *  - [ShadowServiceUnavailable] —— 权限还没给 / 服务没连上。重试一万次也没用，
 *    必须让用户去手机上开辅助功能或授予投屏。端点层会把它映射成
 *    `code = "service_disabled"`，Python 侧对应 `AndroidServiceUnavailable`。
 *  - [ShadowActionFailed] —— 这次操作没成功（手势被取消、目标控件拒绝写入……）。
 *    属于「可以重试或换个策略」的一类。
 *
 * 混成一个类型的话，症状是「用户没开权限，任务却在不停重试」，而界面上什么都不说。
 *
 * （同进程路线 A 下 Python 侧拿到的异常类型名不一定透传得过来——Chaquopy 会把 Java
 * 异常包一层。所以**两条路线的错误信息都必须自带可读的中文原因**，
 * 不能指望上层从类型名反推出该做什么。这一点写在 `android/README.md` 的已知限制里。）
 */
class ShadowServiceUnavailable(message: String) : IllegalStateException(message)

/** 这次设备操作没成功（与「权限没给」区分开）。 */
class ShadowActionFailed(message: String) : IllegalStateException(message)
