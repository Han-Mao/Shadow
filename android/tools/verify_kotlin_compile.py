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

工具链（三个文件，共约 125MB，缺哪个脚本会打出下载命令）：
    JDK 17+                  —— 任意一个都行；本机用 PyCharm 自带的 jbr（设 SHADOW_JAVA）
    kotlin-compiler-embeddable + kotlin-stdlib  —— Maven Central
    android.jar              —— dl.google.com 的 platform-35（与 compileSdk 一致）

用法：
    python android/tools/verify_kotlin_compile.py

环境变量（都有默认值）：
    SHADOW_JAVA          java 可执行文件路径
    SHADOW_KOTLINC_DIR   kotlin-compiler-embeddable / kotlin-stdlib / junit / hamcrest 所在目录
    SHADOW_ANDROID_JAR   android.jar 路径
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ANDROID = REPO / "android"
APP = ANDROID / "app"
RES = APP / "src/main/res"
WORK = REPO / "artifacts/kotlin_verify"  # artifacts/ 在 .gitignore 内

KOTLINC_VERSION = "2.4.20"
COROUTINES_VERSION = "1.10.2"
ANNOTATIONS_VERSION = "24.1.0"
JUNIT_VERSION = "4.13.2"
HAMCREST_VERSION = "1.3"

MAVEN = "https://repo1.maven.org/maven2"
KOTLINC_DIR = Path(os.getenv("SHADOW_KOTLINC_DIR", Path.home() / ".workbuddy/binaries/kotlinc"))
SDK_DIR = Path(os.getenv("SHADOW_SDK_DIR", Path.home() / ".workbuddy/binaries/android-sdk"))
DEFAULT_JAVA = Path("D:/AiApp/pycharm/PyCharm 2026.1/jbr/bin/java.exe")

COMPILER_JAR = KOTLINC_DIR / f"kotlin-compiler-embeddable-{KOTLINC_VERSION}.jar"
STDLIB_JAR = KOTLINC_DIR / f"kotlin-stdlib-{KOTLINC_VERSION}.jar"
COROUTINES_JAR = KOTLINC_DIR / f"kotlinx-coroutines-core-jvm-{COROUTINES_VERSION}.jar"
ANNOTATIONS_JAR = KOTLINC_DIR / f"annotations-{ANNOTATIONS_VERSION}.jar"
JUNIT_JAR = KOTLINC_DIR / f"junit-{JUNIT_VERSION}.jar"
HAMCREST_JAR = KOTLINC_DIR / f"hamcrest-core-{HAMCREST_VERSION}.jar"

DOWNLOADS = {
    COMPILER_JAR: f"{MAVEN}/org/jetbrains/kotlin/kotlin-compiler-embeddable/{KOTLINC_VERSION}/kotlin-compiler-embeddable-{KOTLINC_VERSION}.jar",
    STDLIB_JAR: f"{MAVEN}/org/jetbrains/kotlin/kotlin-stdlib/{KOTLINC_VERSION}/kotlin-stdlib-{KOTLINC_VERSION}.jar",
    COROUTINES_JAR: f"{MAVEN}/org/jetbrains/kotlinx/kotlinx-coroutines-core-jvm/{COROUTINES_VERSION}/kotlinx-coroutines-core-jvm-{COROUTINES_VERSION}.jar",
    ANNOTATIONS_JAR: f"{MAVEN}/org/jetbrains/annotations/{ANNOTATIONS_VERSION}/annotations-{ANNOTATIONS_VERSION}.jar",
    JUNIT_JAR: f"{MAVEN}/junit/junit/{JUNIT_VERSION}/junit-{JUNIT_VERSION}.jar",
    HAMCREST_JAR: f"{MAVEN}/org/hamcrest/hamcrest-core/{HAMCREST_VERSION}/hamcrest-core-{HAMCREST_VERSION}.jar",
}

ANDROID_JAR = Path(os.getenv("SHADOW_ANDROID_JAR", SDK_DIR / "android-35/android.jar"))
PLATFORM_ZIP = SDK_DIR / "platform-35_r01.zip"
PLATFORM_URL = "https://dl.google.com/android/repository/platform-35_r01.zip"

TEST_CLASS = "com.bluewhale.shadow.device.UiTreeSerializerTest"


def find_java() -> str:
    """找一个 JDK 17+。优先环境变量，其次 PyCharm 自带的 jbr（IDE 一定会带）。"""
    candidate = os.getenv("SHADOW_JAVA")
    if candidate:
        return candidate
    if DEFAULT_JAVA.exists():
        return str(DEFAULT_JAVA)
    found = shutil.which("java")
    assert found, (
        "找不到 java。设 SHADOW_JAVA 指向 JDK 17+ 的 java 可执行文件"
        "（没有 JDK 也可以先用 PyCharm/IDEA 自带的 jbr）"
    )
    return found


def check_toolchain() -> list[str]:
    missing = [str(path) for path in DOWNLOADS if not path.exists()]
    if not ANDROID_JAR.exists():
        missing.append(str(ANDROID_JAR))
    return missing


def print_setup_hint(missing: list[str]) -> None:
    print("工具链不齐，缺这些文件：")
    for path in missing:
        print(f"  - {path}")
    print("\n先补齐（Windows Git Bash，代理按需）：")
    print(f'  mkdir -p "{KOTLINC_DIR}"')
    for path, url in DOWNLOADS.items():
        print(f'  curl -L -o "{path}" {url}')
    print(f'  mkdir -p "{SDK_DIR}"')
    print(f'  curl -L -o "{PLATFORM_ZIP}" {PLATFORM_URL}')
    print(f'  # 再跑一次本脚本，它会自动从 zip 里解出 android.jar')


def extract_android_jar() -> None:
    if ANDROID_JAR.exists():
        print(f"[1/3] android.jar 就位 → {ANDROID_JAR}")
        return
    assert PLATFORM_ZIP.exists(), f"缺少 {PLATFORM_ZIP}"
    with zipfile.ZipFile(PLATFORM_ZIP) as zf:
        member = next((n for n in zf.namelist() if n.endswith("android.jar")), None)
        assert member, "platform zip 里没有 android.jar"
        ANDROID_JAR.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(ANDROID_JAR, "wb") as dst:
            shutil.copyfileobj(src, dst)
    print(f"[1/3] 解出 android.jar → {ANDROID_JAR}")


def resource_names() -> dict[str, set[str]]:
    """从 res/ 解析真实的资源名（AAPT 生成 R 的依据）。"""
    names: dict[str, set[str]] = {"string": set(), "id": set(), "layout": set()}

    for values in (RES / "values").glob("*.xml"):
        for raw in re.findall(r'<(?:string|item)[^>]*name="([^"]+)"', values.read_text(encoding="utf-8")):
            name = raw.replace(".", "_")
            # themes.xml 里的 `<item name="android:windowBackground">` 是样式项，不是资源名
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                names["string"].add(name)

    for layout in (RES / "layout").glob("*.xml"):
        names["layout"].add(layout.stem)
        names["id"].update(re.findall(r'android:id="@\+id/([A-Za-z0-9_]+)"', layout.read_text(encoding="utf-8")))

    return names


def one(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    assert match, f"在 build.gradle.kts 里找不到 {pattern}"
    return match.group(1)


def gen_stubs() -> Path:
    """生成 AGP 本该生成的 R 与 BuildConfig。

    名字全部从 `res/` 与 `build.gradle.kts` **真解析**出来，不是手抄——
    这样「代码引用了不存在的资源」会像真实构建那样直接编译失败。
    """
    gradle = (APP / "build.gradle.kts").read_text(encoding="utf-8")
    fields: dict[str, tuple[str, str]] = {
        "VERSION_CODE": ("int", one(gradle, r"versionCode\s*=\s*(\d+)")),
        "VERSION_NAME": ("String", one(gradle, r"versionName\s*=\s*(\S+)")),  # 连同引号一起抓
    }
    for kind, name, value in re.findall(
        r'buildConfigField\(\s*"(\w+)"\s*,\s*"(\w+)"\s*,\s*"([^"]+)"', gradle
    ):
        fields[name] = (kind, f'"{value}"' if kind == "String" else value)

    kotlin_types = {"int": "Int", "long": "Long", "boolean": "Boolean", "String": "String"}
    stubs = WORK / "stubs/com/bluewhale/shadow"
    stubs.mkdir(parents=True, exist_ok=True)

    build_config = ["package com.bluewhale.shadow", "", "// 由 android/tools/verify_kotlin_compile.py 生成（真实构建里由 AGP 生成）。", "object BuildConfig {"]
    for name, (kind, value) in sorted({**fields, "DEBUG": ("boolean", "false"), "BUILD_TYPE": ("String", '"debug"')}.items()):
        build_config.append(f"    const val {name}: {kotlin_types.get(kind, kind)} = {value}")
    build_config.append("}")
    (stubs / "BuildConfig.kt").write_text("\n".join(build_config) + "\n", encoding="utf-8")

    names = resource_names()
    r_lines = ["package com.bluewhale.shadow", "", "// 由 android/tools/verify_kotlin_compile.py 从 res/ 生成（真实构建里由 AAPT 生成）。", "object R {"]
    for kind in ("layout", "id", "string"):
        r_lines.append(f"    object {kind} {{")
        r_lines.extend(f"        const val {name} = {index}" for index, name in enumerate(sorted(names[kind]), start=1))
        r_lines.append("    }")
    r_lines.append("}")
    (stubs / "R.kt").write_text("\n".join(r_lines) + "\n", encoding="utf-8")

    print(
        f"[2/3] 生成 R/BuildConfig 桩（string={len(names['string'])} "
        f"id={len(names['id'])} layout={len(names['layout'])}）"
    )
    return stubs


def run(java: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([java, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def kotlin_home_bits() -> list[str]:
    return [str(p) for p in (COMPILER_JAR, STDLIB_JAR, COROUTINES_JAR, ANNOTATIONS_JAR)]


def compile_sources(java: str, stubs: Path, out_dir: Path) -> bool:
    sources = sorted(str(p) for p in (APP / "src/main/java").rglob("*.kt"))
    sources += sorted(str(p) for p in (APP / "src/test/java").rglob("*.kt"))
    sources += [str(stubs / "R.kt"), str(stubs / "BuildConfig.kt")]
    assert sources, "没找到任何 Kotlin 源码"

    # 桩是作为**源码**一起编的，所以 classpath 只列真正的依赖
    classpath = ";".join(str(p) for p in (STDLIB_JAR, JUNIT_JAR, HAMCREST_JAR, ANDROID_JAR))

    proc = run(java, [
        "-cp", ";".join(kotlin_home_bits()),
        "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
        # 编译器自己的 stdlib 由 -cp 提供，别再让它在“Kotlin home”里找
        "-no-stdlib",
        "-jvm-target", "17",  # 与 app/build.gradle.kts 的 jvmTarget 一致
        "-classpath", classpath,
        "-d", str(out_dir),
        *sources,
    ])
    noise = re.compile(r"^\s+at |^WARNING|sun\.misc\.Unsafe|Please consider reporting")
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip() and not noise.search(line):
            print("   ", line)
    return proc.returncode == 0


def run_unit_tests(java: str, out_dir: Path) -> bool:
    # golden 文件要从 classpath 根找得到（Gradle 里是 src/test/resources 的默认行为）
    shutil.copyfile(APP / "src/test/resources/golden_ui_tree.xml", out_dir / "golden_ui_tree.xml")
    classpath = ";".join(str(p) for p in (out_dir, STDLIB_JAR, JUNIT_JAR, HAMCREST_JAR))
    proc = run(java, ["-cp", classpath, "org.junit.runner.JUnitCore", TEST_CLASS])
    print("   ", "\n    ".join(line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()))
    # JUnitCore 成功时会打 `OK (N tests)`；有失败则打 `FAILURES!!!` 且返回码非 0
    return proc.returncode == 0 and "OK (" in proc.stdout


def main() -> int:
    missing = check_toolchain()
    if missing:
        print_setup_hint(missing)
        return 2

    java = find_java()
    print(f"java         {java}")
    extract_android_jar()
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
