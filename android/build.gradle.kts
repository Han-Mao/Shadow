// Shadow Android —— 设备端点（部署路线 B，见 README）
//
// 这个工程默认**不依赖任何 Python**：它是一个「把手机本身变成 Shadow 可驱动的设备」的应用，
// 对外暴露 android/README.md 里定义的那 12 个能力。Shadow Core 跑在 PC / 局域网，
// 通过 SHADOW_ANDROID_BRIDGE_URL 连过来。
//
// 路线 A（把 Python Core 也塞进 APK，Chaquopy）需要额外两步：给这个文件加
// com.chaquo.python 插件、给 app/build.gradle.kts 加 chaquopy 块。README 的
// 「路线选择」一节写了确切片段，以及目前挡在那条路上的那个依赖（pydantic v2）。
// 之所以不默认打开：它会让**每一次**构建都必须解析 chaquo 仓库，而路线 B 不需要。

plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
}
