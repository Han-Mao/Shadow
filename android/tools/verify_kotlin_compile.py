"""在没有 Android Studio / Gradle 的机器上，把 `android/` 的 Kotlin 源码真编译一遍。

为什么这件事值得单独有个脚本：`UiTreeSerializer` 吐出的 XML 是跨语言契约，
它错了 Python 侧会静默降级（按钮找不到、危险动作被判成安全）。静态扫描能挡住
「属性名写错」，但挡不住「代码根本编译不过」——本轮就靠真编译抓到两处：

  * `ShadowAccessibilityService.globalAction()` 里的 `require()` 撞上 Kotlin 标准库的
    `kotlin.require(Boolean)`（本类没有同名成员），报错信息完全指不到问题所在；
  * `findFocus(FOCUS_INPUT)` 少了类名限定——`FOCUS_INPUT` 是 `AccessibilityNodeInfo`
    的常量，不是 `AccessibilityService` 的。

这两处都只会在真机上暴露（而且表现为「返回/回桌面莫名失败」「输入找不到焦点框」），
所以「没有 SDK 就编不了」并不成立——本工程**零 androidx 依赖**，
Kotlin 侧只用 `android.*` / `java.*` / `org.json`，一个 `android.jar` 就够。

工具链的定位、下载、缺件提示都在 `_toolchain.py`（与 `build_apk.py` 共用）。

用法：
    python android/tools/verify_kotlin_compile.py [--no-download]

    --no-download   缺工具链时不自动下载，只打印手工下载命令
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from _toolchain import (  # noqa: E402 - 同目录的兄弟模块
    ANDROID_JAR,
    APP,
    COMPILER_JAR,
    COROUTINES_JAR,
    JUNIT_JAR,
    HAMCREST_JAR,
    REPO,
    STDLIB_JAR,
    ANDROID,
    ensure_toolchain,
    find_java,
    kotlin_home_bits,
    print_setup_hint,
    resource_names,
    run_java,
)

WORK = REPO / "artifacts/kotlin_verify"  # artifacts/ 在 .gitignore 内

TEST_CLASS = "com.bluewhale.shadow.device.UiTreeSerializerTest"


def gen_stubs() -> Path:
    """生成 AGP 本该生成的 R 与 BuildConfig。

    名字全部从 `res/` 与 `build.gradle.kts` **真解析**出来（见 `_toolchain`），
    不是手抄——这样「代码引用了不存在的资源」会像真实构建那样直接编译失败。

    注意这里是 **Kotlin** 桩（`object R`），因为本脚本把桩当源码一起交给 kotlinc；
    打 APK 的那条路上用的是 AAPT 真生成的 `R.java` + 我们自己生成的 `BuildConfig.java`
    （与 AGP 一致），见 `build_apk.py`。
    """
    from _toolchain import gradle_app_config

    config = gradle_app_config()
    fields = dict(config["build_config"])
    fields["DEBUG"] = ("boolean", "false")
    fields["BUILD_TYPE"] = ("String", '"debug"')

    stubs = WORK / "stubs/com/bluewhale/shadow"
    stubs.mkdir(parents=True, exist_ok=True)

    kotlin_types = {"int": "Int", "long": "Long", "boolean": "Boolean", "String": "String"}
    build_config = [
        "package com.bluewhale.shadow",
        "",
        "// 由 android/tools/verify_kotlin_compile.py 生成（真实构建里由 AGP 生成）。",
        "object BuildConfig {",
    ]
    for name, (kind, value) in sorted(fields.items()):
        build_config.append(f"    const val {name}: {kotlin_types.get(kind, kind)} = {value}")
    build_config.append("}")
    (stubs / "BuildConfig.kt").write_text("\n".join(build_config) + "\n", encoding="utf-8")

    names = resource_names()
    r_lines = [
        "package com.bluewhale.shadow",
        "",
        "// 由 android/tools/verify_kotlin_compile.py 从 res/ 生成（真实构建里由 AAPT 生成）。",
        "object R {",
    ]
    for kind in ("layout", "id", "string"):
        r_lines.append(f"    object {kind} {{")
        r_lines.extend(
            f"        const val {name} = {index}"
            for index, name in enumerate(sorted(names[kind]), start=1)
        )
        r_lines.append("    }")
    r_lines.append("}")
    (stubs / "R.kt").write_text("\n".join(r_lines) + "\n", encoding="utf-8")

    print(
        f"[2/3] 生成 R/BuildConfig 桩（string={len(names['string'])} "
        f"id={len(names['id'])} layout={len(names['layout'])}）"
    )
    return stubs


def compile_sources(java: str, stubs: Path, out_dir: Path) -> bool:
    sources = sorted(str(p) for p in (APP / "src/main/java").rglob("*.kt"))
    sources += sorted(str(p) for p in (APP / "src/test/java").rglob("*.kt"))
    sources += [str(stubs / "R.kt"), str(stubs / "BuildConfig.kt")]
    assert sources, "没找到任何 Kotlin 源码"

    # 桩是作为**源码**一起编的，所以 classpath 只列真正的依赖
    classpath = ";".join(str(p) for p in (STDLIB_JAR, JUNIT_JAR, HAMCREST_JAR, ANDROID_JAR))

    proc = run_java(
        java,
        [
            "-cp",
            ";".join(kotlin_home_bits()),
            "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
            # 编译器自己的 stdlib 由 -cp 提供，别再让它在“Kotlin home”里找
            "-no-stdlib",
            "-jvm-target",
            "17",  # 与 app/build.gradle.kts 的 jvmTarget 一致
            "-classpath",
            classpath,
            "-d",
            str(out_dir),
            *sources,
        ],
    )
    noise = re.compile(r"^\s+at |^WARNING|sun\.misc\.Unsafe|Please consider reporting")
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip() and not noise.search(line):
            print("   ", line)
    return proc.returncode == 0


def run_unit_tests(java: str, out_dir: Path) -> bool:
    # golden 文件要从 classpath 根找得到（Gradle 里是 src/test/resources 的默认行为）
    shutil.copyfile(APP / "src/test/resources/golden_ui_tree.xml", out_dir / "golden_ui_tree.xml")
    classpath = ";".join(str(p) for p in (out_dir, STDLIB_JAR, JUNIT_JAR, HAMCREST_JAR))
    proc = run_java(java, ["-cp", classpath, "org.junit.runner.JUnitCore", TEST_CLASS])
    print("   ", "\n    ".join(line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()))
    # JUnitCore 成功时会打 `OK (N tests)`；有失败则打 `FAILURES!!!` 且返回码非 0
    return proc.returncode == 0 and "OK (" in proc.stdout


def main() -> int:
    missing = ensure_toolchain(download_missing="--no-download" not in sys.argv)
    if missing:
        print_setup_hint(missing)
        return 2

    java = find_java()
    print(f"java         {java}")
    if ANDROID_JAR.exists():
        print(f"[1/3] android.jar 就位 → {ANDROID_JAR}")
    stubs = gen_stubs()

    out_dir = WORK / "out"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[3/3] 编译全部 Kotlin 源码 …")
    if not compile_sources(java, stubs, out_dir):
        print("\n编译失败。")
        return 1
    classes = len(list(out_dir.rglob("*.class")))
    print(f"      编译通过：{classes} 个 class")

    print("运行 JVM 单测 …")
    if not run_unit_tests(java, out_dir):
        print("\n单测失败。")
        return 1

    print(f"\n全部通过（编译 {classes} 个 class + JVM 单测）。")
    print(f"想打成可安装的 APK：python {ANDROID.name}/tools/build_apk.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
