"""在没有 Android Studio / Gradle / Android SDK 的机器上，打一只**可安装**的 debug APK。

`verify_kotlin_compile.py` 证明的是「能编译、单测过」；它**不产出 APK**。而这个包要真上手机，
差的正是 AAPT 资源打包 / dex / 签名这三层——也就是 `assembleDebug` 那一步。
本脚本用 build-tools 里那几个可独立运行的工具把这三层手工走一遍，产出与
Android Studio 的 debug 构建同形的 APK（debug 签名、`debuggable=true`、`DEBUG=true`）。

    aapt2 compile     res/ → .flat（编译资源）
    aapt2 link        清单 + 资源 + android.jar → base.apk（含 resources.arsc）+ R.java
    javac             R.java + BuildConfig.java
    kotlinc           src/main 的 Kotlin 源码（classpath = android.jar + R/BuildConfig）
    d8                全部 class → classes.dex
    zip 装包          base.apk + classes.dex → unsigned.apk
    zipalign          4 字节对齐（-p 顺带页对齐 .so，虽然本工程没有 .so）
    apksigner         调试密钥签名（v1 + v2）
    apksigner verify  校验签名
    aapt2 dump badging 打印包名/版本/权限/组件，并逐条核对「清单声明的组件都在 dex 里」

用法：
    python android/tools/build_apk.py [--no-download]

    --no-download   缺工具链时不自动下载，只打印手工下载命令

产物：
    android/app/build/outputs/apk/debug/app-debug.apk   ← 与 AGP 的路径一致，
                                                          所以 README 里那句
                                                          `adb install -r ...` 直接可用
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sys
import zipfile
from pathlib import Path

from _toolchain import (  # noqa: E402 - 同目录的兄弟模块
    ANDROID_JAR,
    APP,
    ANDROID,
    COROUTINES_JAR,
    MANIFEST,
    REPO,
    RES,
    STDLIB_JAR,
    banner,
    build_tool,
    ensure_toolchain,
    find_java,
    find_keytool,
    gradle_app_config,
    kotlin_home_bits,
    print_setup_hint,
    run,
    run_java,
)

WORK = REPO / "artifacts/android_apk"  # artifacts/ 在 .gitignore 内
OUT_APK = APP / "build/outputs/apk/debug/app-debug.apk"
# 调试密钥**不能**放 `WORK` 里：`main()` 开头会 `rmtree(WORK)`，那样每次构建都生成
# 一把新密钥、签名随之改变，于是 `adb install -r` 必然撞上
# `INSTALL_FAILED_UPDATE_INCOMPATIBLE`，只能先卸载再装（2026-09-16 真机踩到）。
# 用 AGP 的惯例位置 `~/.android/debug.keystore`：跨构建稳定，且与
# `./gradlew assembleDebug` 共用同一把密钥 —— 两条构建路径产出的包可以互相覆盖安装。
KEYSTORE = Path.home() / ".android" / "debug.keystore"
KEY_ALIAS = "androiddebugkey"
KEY_PASS = "android"


# ---------------------------------------------------------------- 清单


def merged_manifest(config: dict) -> Path:
    """生成「合并后」的清单——AGP 的 manifest merger 在 debug 构建里做的注入。

    注入两件事，都是 AGP 的默认行为，只是这里由我们显式做：

    1. `package="…"`：`build.gradle.kts` 里的 `namespace` 由 AGP 注入清单。
       AAPT 需要一个包名来生成 `R`，源码里 `R.id.x` 的包路径也由它决定。
    2. `android:debuggable="true"`：debug 构建要有它（`run-as`、logcat 调试都靠它）。

    **不做静默的字符串替换**：两次 `assert` 保证它们真的改到了，改不到就当场失败——
    清单结构一变就出声，免得打出一个人格分裂的包（比如 debuggable 没生效）。
    """
    text = MANIFEST.read_text(encoding="utf-8")

    head, sep, rest = text.partition("<manifest")
    assert sep, "AndroidManifest.xml 里没有 <manifest>"
    end = rest.index(">")
    tag = rest[:end]
    assert "package=" not in tag, "清单已经带 package 属性了——本脚本的注入前提变了"
    text = head + sep + tag + f'\n    package="{config["application_id"]}"' + rest[end:]

    head, sep, rest = text.partition("<application")
    assert sep, "AndroidManifest.xml 里没有 <application>"
    end = rest.index(">")
    tag = rest[:end]
    assert "debuggable" not in tag, "清单已经带 debuggable 属性了——注入前提变了"
    text = head + sep + tag + '\n        android:debuggable="true"' + rest[end:]

    target = WORK / "AndroidManifest.xml"
    target.write_text(text, encoding="utf-8")
    return target


def manifest_components(manifest_text: str, package: str) -> list[str]:
    """清单里声明的 activity / service 的**全限定类名**（`.Foo` 用包名补全）。

    用它去 dex 里找对应的类，是「改名忘了改清单」这类错误的最后一道闸：
    AGP 会把这类问题留到安装/启动时才炸（`ClassNotFoundException`），
    而这里在打包阶段就能看出来。
    """
    names = re.findall(r'android:name="(\.[A-Za-z0-9_.]+)"', manifest_text)
    return [f"{package}{name}" if name.startswith(".") else name for name in names]


# ---------------------------------------------------------------- 构建步骤


def generate_build_config(config: dict) -> Path:
    """生成 `BuildConfig.java`——AGP 在 `buildFeatures.buildConfig = true` 时生成的那个。

    值来自 `build.gradle.kts`（版本号、`buildConfigField`），且 `DEBUG` / `BUILD_TYPE`
    按 debug 构建填：这个包会被 `debuggable=true` + 调试密钥签名，说自己是 release 才是错的。
    """
    fields: dict[str, tuple[str, str]] = {
        "DEBUG": ("boolean", "true"),
        "BUILD_TYPE": ("String", '"debug"'),
        "APPLICATION_ID": ("String", f'"{config["application_id"]}"'),
    }
    fields.update(config["build_config"])

    java_types = {"int": "int", "long": "long", "boolean": "boolean", "String": "String"}
    lines = [
        "package com.bluewhale.shadow;",
        "",
        "// 由 android/tools/build_apk.py 生成（真实构建里由 AGP 生成）。",
        "public final class BuildConfig {",
        "    private BuildConfig() {}",
    ]
    for name, (kind, value) in sorted(fields.items()):
        lines.append(f"    public static final {java_types.get(kind, kind)} {name} = {value};")
    lines.append("}")
    # 位置必须与包名一致：aapt2 的 R.java 也在 <namespace>/ 下，javac 靠目录结构找包
    target = WORK / "gen/com/bluewhale/shadow/BuildConfig.java"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def compile_resources(aapt2: Path) -> Path:
    """`aapt2 compile`：res/ 下每个资源编译成 `.flat`，打进一个 zip（`--dir` 模式）。"""
    out = WORK / "res.zip"
    proc = run(str(aapt2), ["compile", "--dir", str(RES), "-o", str(out)])
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        raise SystemExit("aapt2 compile 失败")
    return out


def link_resources(aapt2: Path, manifest: Path, res_zip: Path, config: dict) -> Path:
    """`aapt2 link`：产出带 `resources.arsc` 的 base.apk + 真正的 `R.java`。

    资源包**走位置参数**而不是 `-R`：`aapt2 link -h` 里写得很清楚——
    `-R` 是「**overlay 语义**，最后给出的冲突资源胜出」。用它传普通资源，
    第一份资源就成了 overlay，aapt2 会逐条抱怨
    `resource string/app_name does not override an existing resource`
    （除非再加 `--auto-add-overlay` 把重复定义静默掉——那等于关掉重复检测）。
    AGP 也是把 `.flat` 当位置参数传的。

    `--min-sdk-version` / `--target-sdk-version` 由我们传（AGP 也是这么做的）：
    清单里没写这两项，APK 的兼容区间与 R 的生成都靠它们。
    """
    gen = WORK / "gen"
    gen.mkdir(parents=True, exist_ok=True)
    base = WORK / "base.apk"
    proc = run(
        str(aapt2),
        [
            "link",
            "-o", str(base),
            "-I", str(ANDROID_JAR),
            "--manifest", str(manifest),
            "--java", str(gen),
            "--min-sdk-version", config["min_sdk"],
            "--target-sdk-version", config["target_sdk"],
            "--version-code", config["version_code"],
            "--version-name", config["version_name"],
            str(res_zip),
        ],
    )
    if proc.returncode != 0:
        print("\n".join((proc.stdout + proc.stderr).splitlines()[:20]))
        raise SystemExit("aapt2 link 失败")
    r_java = gen / "com/bluewhale/shadow/R.java"
    assert r_java.exists(), f"aapt2 没有生成 R.java（找的是 {r_java}）"
    return r_java


def compile_java(java: str, *sources: Path) -> Path:
    """`javac` 编 R + BuildConfig。`--release 17` 与 `build.gradle.kts` 的 Java 17 对齐。"""
    javac = Path(java).with_name("javac.exe" if java.endswith(".exe") else "javac")
    assert javac.exists(), f"找不到 javac（在 {javac}）——它应当与 java 同目录"
    out = WORK / "classes_java"
    proc = run(
        str(javac),
        ["--release", "17", "-cp", str(ANDROID_JAR), "-d", str(out), *[str(p) for p in sources]],
    )
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        raise SystemExit("javac 失败")
    return out


def compile_kotlin(java: str, java_classes: Path) -> Path:
    """`kotlinc` 编 src/main 的 Kotlin 源码（**不含** src/test，那是 JVM 单测的事）。"""
    sources = sorted(str(p) for p in (APP / "src/main/java").rglob("*.kt"))
    assert sources, "没找到 Kotlin 源码"
    out = WORK / "classes_kt"
    out.mkdir(parents=True, exist_ok=True)
    classpath = ";".join(
        str(p) for p in (ANDROID_JAR, STDLIB_JAR, COROUTINES_JAR, java_classes)
    )
    proc = run_java(
        java,
        [
            "-cp", ";".join(kotlin_home_bits()),
            "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
            "-no-stdlib",
            "-jvm-target", "17",
            "-classpath", classpath,
            "-d", str(out),
            *sources,
        ],
    )
    noise = re.compile(r"^\s+at |^WARNING|sun\.misc\.Unsafe|Please consider reporting")
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip() and not noise.search(line):
            print("   ", line)
    if proc.returncode != 0:
        raise SystemExit("kotlinc 失败")
    return out


def dex(java: str, *class_dirs: Path) -> Path:
    """`d8` 把全部 class 变成 `classes.dex`（`--lib android.jar` 才能做脱糖）。

    传的是**显式列出的 `.class` 文件**，不是目录：kotlinc 会在输出目录里放一个
    `META-INF/main.kotlin_module`，而 D8 遇到目录时会连带扫到它并报
    `Unsupported source file type` ——一个和真正原因毫无关系的错误信息。

    **还要把 `kotlin-stdlib.jar` 一起 dex。** Kotlin 编出来的字节码会调用
    `kotlin.jvm.internal.Intrinsics` 这一类**不在源码里**的类（空值检查、默认参数、
    数据类的 `equals` 等都由它实现），它们来自标准库。编译期靠 `-classpath` 能找到，
    但**运行期**必须真的在 dex 里，否则一启动就是：

        java.lang.NoClassDefFoundError: Failed resolution of: Lkotlin/jvm/internal/Intrinsics;

    AGP 由 `kotlin-stdlib` 依赖自动完成这一步，手写链路必须显式做。这个坑的代价很高
    ——漏了它，包能打、能签名、能安装，`verify_contents` 那套静态校验也会全过
    （它查的是「项目自己的类在不在」），**只有真机点开图标的那一刻才崩**。
    """
    out = WORK / "dex"
    out.mkdir(parents=True, exist_ok=True)
    classes = sorted(p for directory in class_dirs for p in directory.rglob("*.class"))
    assert classes, f"没有任何 .class：{[str(d) for d in class_dirs]}"
    proc = run_java(
        java,
        [
            "-Xmx1024M", "-Xss1m",
            "-cp", str(build_tool("d8_jar")),
            "com.android.tools.r8.D8",
            "--min-api", "26",
            "--lib", str(ANDROID_JAR),
            "--output", str(out),
            *[str(p) for p in classes],
            # 运行时库：d8 接受 jar，会把它里面的 class 一并收进 classes.dex。
            # 源码目前没有用 kotlinx.coroutines（只用了一次 kotlin.concurrent.thread，
            # 那是 stdlib 里的），所以只需要 stdlib 这一份。
            str(STDLIB_JAR),
        ],
    )
    if proc.returncode != 0:
        print("\n".join((proc.stdout + proc.stderr).splitlines()[:20]))
        raise SystemExit("d8 失败")
    dex_file = out / "classes.dex"
    assert dex_file.exists(), "d8 没有产出 classes.dex"
    print(f"      {len(classes)} 个 class → classes.dex")
    return dex_file


def package_apk(base: Path, dex_file: Path) -> Path:
    """把 `classes.dex` 塞进 aapt2 打好的 base.apk（它就是缺 dex 的 APK）。"""
    out = WORK / "unsigned.apk"
    shutil.copyfile(base, out)
    with zipfile.ZipFile(out, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.write(dex_file, "classes.dex")
    return out


def zipalign(zipalign_bin: Path, unsigned: Path) -> Path:
    out = WORK / "aligned.apk"
    proc = run(str(zipalign_bin), ["-f", "-p", "4", str(unsigned), str(out)])
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        raise SystemExit("zipalign 失败")
    return out


def ensure_keystore(keytool: str) -> Path:
    """调试签名用的 keystore —— 位置与理由见 `KEYSTORE` 处的注释。

    固定口令 + 固定别名是 Android 工具链的既有约定（`~/.android/debug.keystore` 就是
    `android` / `androiddebugkey` / `android`），所以机器上已有 Android Studio 生成的那一份时
    **直接复用**，两条构建路径产出的包可以互相覆盖安装。

    它只是**调试**密钥：不在工作区内、签不进仓库，也不会被误当成发布密钥。
    """
    # `~/.android/` 在新机器上不一定存在（旧位置 WORK 是 main() 里建好的）。
    KEYSTORE.parent.mkdir(parents=True, exist_ok=True)
    if KEYSTORE.exists():
        return KEYSTORE
    proc = run(
        keytool,
        [
            "-genkeypair", "-keystore", str(KEYSTORE), "-alias", KEY_ALIAS,
            "-storepass", KEY_PASS, "-keypass", KEY_PASS,
            "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
            "-dname", "CN=Android Debug,O=Android,C=US",
        ],
    )
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        raise SystemExit("生成调试 keystore 失败")
    return KEYSTORE


def sign(java: str, aligned: Path) -> Path:
    """`apksigner sign`：v1 + v2 都签（默认行为），产物直接写到 AGP 那个路径。"""
    OUT_APK.parent.mkdir(parents=True, exist_ok=True)
    if OUT_APK.exists():
        OUT_APK.unlink()
    proc = run_java(
        java,
        [
            "-Xmx1024M", "-Xss1m",
            "-jar", str(build_tool("apksigner_jar")),
            "sign",
            "--ks", str(KEYSTORE), "--ks-key-alias", KEY_ALIAS,
            "--ks-pass", f"pass:{KEY_PASS}", "--key-pass", f"pass:{KEY_PASS}",
            "--out", str(OUT_APK), str(aligned),
        ],
        timeout=300,
    )
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        raise SystemExit("apksigner 签名失败")
    return OUT_APK


# ---------------------------------------------------------------- 校验


def verify_signature(java: str, apk: Path) -> None:
    proc = run_java(
        java,
        ["-Xmx1024M", "-jar", str(build_tool("apksigner_jar")), "verify", "--verbose", str(apk)],
        timeout=300,
    )
    print(proc.stdout.strip() or proc.stderr.strip())
    assert proc.returncode == 0, "apksigner verify 未通过"


def verify_contents(aapt2: Path, apk: Path, manifest_text: str, package: str) -> None:
    """逐条核对「包该有的东西都在」——这是没有真机时能达到的最强验证。

    查六样：
    ① `dexdump` 能解析 `classes.dex`（容器没坏、能读出 class 数）；
    ② 清单里的每个组件类都在 dex 里（`ClassNotFoundException` 的替身）；
    ③ 三个**不在清单里但少了就是废包**的类也在（桥、端点、序列化器）；
    ④ `kotlin.jvm.internal.Intrinsics` 在 dex 里，即 kotlin-stdlib 真的被收进来了
       ——编译期有 `-classpath` 就够，**运行期必须物理存在**（见 `dex()`）；
    ⑤ `resources.arsc` 与辅助功能配置 XML 在（少了前者启动就崩；少了后者
       `resource-id` 全空 → 风险判定会**静默变松**）；
    ⑥ badging 里的包名/版本/权限与源码一致。
    """
    with zipfile.ZipFile(apk) as zf:
        entries = set(zf.namelist())
        dex = zf.read("classes.dex")

    for required in ("AndroidManifest.xml", "resources.arsc", "classes.dex"):
        assert required in entries, f"APK 里缺少 {required}"
    assert "res/xml/shadow_accessibility_service.xml" in entries, "辅助功能配置没打进去"

    # dexdump 只认裸的 .dex（给它 APK 会报 `Couldn't get file size`）。
    # 所以把**包里那份**解出来再验——验的是真正会装到手机上的字节，
    # 而不是我们本地那份中间产物。
    packaged_dex = WORK / "verify-classes.dex"
    packaged_dex.write_bytes(dex)
    header = run(str(build_tool("dexdump")), ["-f", str(packaged_dex)])
    assert header.returncode == 0, f"dexdump 读不了 classes.dex：{header.stderr[:200]}"
    print("      " + "\n      ".join(header.stdout.strip().splitlines()[:4]))

    # 这三个类不在清单里（清单只声明 activity / service），但少了任何一个这个包都用不了。
    # 它们正是与 Python 侧对接口的那几处，所以单独点名。
    core = [
        f"{package}.device.AndroidBridgeImpl",
        f"{package}.device.UiTreeSerializer",
        f"{package}.endpoint.BridgeHttpServer",
    ]
    missing = []
    for name in manifest_components(manifest_text, package) + core:
        descriptor = "L" + name.replace(".", "/") + ";"
        if descriptor.encode() not in dex:
            missing.append(name)
    assert not missing, f"这些类不在 dex 里：{missing}"

    # 第 ④ 条：Kotlin 运行时库。见 `dex()` 的 docstring —— 「编译期能找到」不等于
    # 「打进了 dex」。漏了它时，上面每一条检查都会通过、包也能正常安装，**只有真机
    # 点开图标的那一刻才崩**（2026-09-16 在 vivo V2352A / Android 16 上踩到）。
    # 查具体类名而不是「有没有 Lkotlin/ 前缀」：Intrinsics 是每个 Kotlin 文件都会调的，
    # 它缺席就一定是标准库没进来，报错信息也能直接指向修法。
    assert b"Lkotlin/jvm/internal/Intrinsics;" in dex, (
        "dex 里没有 kotlin.jvm.internal.Intrinsics —— kotlin-stdlib 没被 d8 收进去。\n"
        "  症状：包能打、能签名、能安装，静态检查全过，真机一点开图标就崩：\n"
        "    NoClassDefFoundError: Failed resolution of: Lkotlin/jvm/internal/Intrinsics;\n"
        "  修法：`dex()` 里把 STDLIB_JAR 作为 d8 的输入之一"
        "（AGP 由 kotlin-stdlib 依赖自动完成这一步）。"
    )

    badging = run(str(aapt2), ["dump", "badging", str(apk)])
    text = badging.stdout
    assert f"package: name='{package}'" in text, f"badging 里的包名不对：{text.splitlines()[0]}"
    for permission in (
        "android.permission.INTERNET",
        "android.permission.FOREGROUND_SERVICE",
        "android.permission.POST_NOTIFICATIONS",
        "android.permission.QUERY_ALL_PACKAGES",
    ):
        assert permission in text, f"badging 里没有 {permission}"
    print(text.strip()[:1200])


def main() -> int:
    missing = ensure_toolchain(with_build_tools=True, download_missing="--no-download" not in sys.argv)
    if missing:
        print_setup_hint(missing)
        return 2

    java = find_java()
    keytool = find_keytool()
    aapt2 = build_tool("aapt2")
    config = gradle_app_config()
    package = config["application_id"]

    print(f"java         {java}")
    banner(str(aapt2), ["version"])

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True, exist_ok=True)

    print(f"[1/9] 合并清单（package={package}，debuggable=true）")
    manifest = merged_manifest(config)
    manifest_text = manifest.read_text(encoding="utf-8")
    components = manifest_components(manifest_text, package)
    print(f"      清单声明 {len(components)} 个组件：{'、'.join(c.split('.')[-1] for c in components)}")

    print("[2/9] aapt2 compile：res/ → .flat")
    res_zip = compile_resources(aapt2)

    print("[3/9] aapt2 link：清单 + 资源 → base.apk + R.java")
    r_java = link_resources(aapt2, manifest, res_zip, config)

    print("[4/9] javac：R.java + BuildConfig.java")
    build_config = generate_build_config(config)
    java_classes = compile_java(java, r_java, build_config)

    print("[5/9] kotlinc：src/main 的 Kotlin 源码")
    kotlin_classes = compile_kotlin(java, java_classes)

    print("[6/9] d8：class → classes.dex")
    dex_file = dex(java, java_classes, kotlin_classes)
    print(f"      classes.dex {dex_file.stat().st_size / 1024:.0f} KB")

    print("[7/9] 装包 + zipalign")
    unsigned = package_apk(WORK / "base.apk", dex_file)
    aligned = zipalign(build_tool("zipalign"), unsigned)

    print("[8/9] 签名（调试密钥）")
    ensure_keystore(keytool)
    apk = sign(java, aligned)

    print("[9/9] 校验")
    verify_signature(java, apk)
    verify_contents(aapt2, apk, manifest_text, package)

    digest = hashlib.sha256(apk.read_bytes()).hexdigest()
    print(f"\n产物：{apk}")
    print(f"      大小 {apk.stat().st_size / 1024:.0f} KB，sha256 {digest[:16]}…")
    print(f"      安装：adb install -r \"{apk}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
