"""`android/tools/` 下几个脚本共用的：工具链定位/下载 + 构建元数据解析。

抽出来的原因很实际：`verify_kotlin_compile.py`（真编译 + JVM 单测）与
`build_apk.py`（打可安装的 APK）用的是**同一套** JDK / android.jar / kotlinc，
而且都要从 `build.gradle.kts` 与 `res/` 里解析出「R 与 BuildConfig 该有什么名字」。
这些写两份，早晚会漂移成「一个脚本能跑、另一个说缺文件」或者
「单测过了、APK 里的资源名不对」。

工具链全在 `~/.workbuddy/binaries/` 下缓存，**不装进系统目录**，也不进 git：
    kotlinc/               kotlin-compiler-embeddable + stdlib + coroutines + junit
    android-sdk/           platform-35 的 android.jar（compileSdk 35）
    android-build-tools/   build-tools r35：aapt2 / d8 / zipalign / apksigner / dexdump

环境变量（都有默认值，不需要设）：
    SHADOW_JAVA               java 可执行文件；不设则用 PyCharm 自带的 jbr
    SHADOW_KOTLINC_DIR        kotlinc 那几个 jar 所在目录
    SHADOW_SDK_DIR            android.jar 所在目录
    SHADOW_ANDROID_JAR        直接指定 android.jar
    SHADOW_BUILD_TOOLS_DIR    build-tools 所在目录
    SHADOW_DOWNLOAD_PROXY     下载时用的代理（默认 http://127.0.0.1:7897）
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ANDROID = REPO / "android"
APP = ANDROID / "app"
RES = APP / "src/main/res"
MANIFEST = APP / "src/main/AndroidManifest.xml"
GRADLE_APP = APP / "build.gradle.kts"

HOME_BINARIES = Path.home() / ".workbuddy/binaries"

# ---- Kotlin 编译器与测试依赖（Maven Central）----
KOTLINC_VERSION = "2.4.20"
COROUTINES_VERSION = "1.10.2"
ANNOTATIONS_VERSION = "24.1.0"
JUNIT_VERSION = "4.13.2"
HAMCREST_VERSION = "1.3"
MAVEN = "https://repo1.maven.org/maven2"

KOTLINC_DIR = Path(os.getenv("SHADOW_KOTLINC_DIR", HOME_BINARIES / "kotlinc"))
COMPILER_JAR = KOTLINC_DIR / f"kotlin-compiler-embeddable-{KOTLINC_VERSION}.jar"
STDLIB_JAR = KOTLINC_DIR / f"kotlin-stdlib-{KOTLINC_VERSION}.jar"
COROUTINES_JAR = KOTLINC_DIR / f"kotlinx-coroutines-core-jvm-{COROUTINES_VERSION}.jar"
ANNOTATIONS_JAR = KOTLINC_DIR / f"annotations-{ANNOTATIONS_VERSION}.jar"
JUNIT_JAR = KOTLINC_DIR / f"junit-{JUNIT_VERSION}.jar"
HAMCREST_JAR = KOTLINC_DIR / f"hamcrest-core-{HAMCREST_VERSION}.jar"

MAVEN_DOWNLOADS = {
    COMPILER_JAR: f"{MAVEN}/org/jetbrains/kotlin/kotlin-compiler-embeddable/{KOTLINC_VERSION}/kotlin-compiler-embeddable-{KOTLINC_VERSION}.jar",
    STDLIB_JAR: f"{MAVEN}/org/jetbrains/kotlin/kotlin-stdlib/{KOTLINC_VERSION}/kotlin-stdlib-{KOTLINC_VERSION}.jar",
    COROUTINES_JAR: f"{MAVEN}/org/jetbrains/kotlinx/kotlinx-coroutines-core-jvm/{COROUTINES_VERSION}/kotlinx-coroutines-core-jvm-{COROUTINES_VERSION}.jar",
    ANNOTATIONS_JAR: f"{MAVEN}/org/jetbrains/annotations/{ANNOTATIONS_VERSION}/annotations-{ANNOTATIONS_VERSION}.jar",
    JUNIT_JAR: f"{MAVEN}/junit/junit/{JUNIT_VERSION}/junit-{JUNIT_VERSION}.jar",
    HAMCREST_JAR: f"{MAVEN}/org/hamcrest/hamcrest-core/{HAMCREST_VERSION}/hamcrest-core-{HAMCREST_VERSION}.jar",
}

# ---- 平台（android.jar，compileSdk 35）----
SDK_DIR = Path(os.getenv("SHADOW_SDK_DIR", HOME_BINARIES / "android-sdk"))
ANDROID_JAR = Path(os.getenv("SHADOW_ANDROID_JAR", SDK_DIR / "android-35/android.jar"))
PLATFORM_ZIP = SDK_DIR / "platform-35_r01.zip"
PLATFORM_URL = "https://dl.google.com/android/repository/platform-35_r01.zip"

# ---- build-tools（aapt2 / d8 / zipalign / apksigner / dexdump）----
#
# r35 与 compileSdk 35 对齐。为什么不装 Android Studio：这里只需要这五个可执行文件，
# 而 SDK Manager 会顺带装上模拟器、平台镜像等用不到的东西。
BUILD_TOOLS_VERSION = "35.0.0"
BUILD_TOOLS_DIR = Path(
    os.getenv("SHADOW_BUILD_TOOLS_DIR", HOME_BINARIES / "android-build-tools")
)
BUILD_TOOLS_HOME = BUILD_TOOLS_DIR / BUILD_TOOLS_VERSION
BUILD_TOOLS_ZIP = BUILD_TOOLS_DIR / "build-tools_r35_windows.zip"
BUILD_TOOLS_URL = "https://dl.google.com/android/repository/build-tools_r35_windows.zip"

# build-tools 里每个工具在 zip 内的相对路径（zip 顶层目录名与版本无关，解压时被剥掉）
BUILD_TOOLS_FILES = {
    "aapt2": "aapt2.exe",
    "zipalign": "zipalign.exe",
    "dexdump": "dexdump.exe",
    "apksigner_jar": "lib/apksigner.jar",
    "d8_jar": "lib/d8.jar",
}

DEFAULT_JAVA = Path("D:/AiApp/pycharm/PyCharm 2026.1/jbr/bin/java.exe")
DEFAULT_KEYTOOL = DEFAULT_JAVA.with_name("keytool.exe")


def proxy_url() -> str | None:
    """下载走的代理。默认主机代理 7897：沙箱代理连不上 dl.google.com / Maven Central。"""
    explicit = os.getenv("SHADOW_DOWNLOAD_PROXY")
    if explicit is not None:
        return explicit or None
    return "http://127.0.0.1:7897"


def opener():
    proxy = proxy_url()
    if not proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )


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


def find_keytool() -> str:
    """签名用的 keytool——它和 java 同目录，所以优先顺着 java 找。"""
    java = Path(find_java())
    candidate = java.with_name("keytool.exe" if os.name == "nt" else "keytool")
    if candidate.exists():
        return str(candidate)
    found = shutil.which("keytool")
    assert found, "找不到 keytool（生成调试签名用），它应当与 java 同目录"
    return found


def run(tool: str, args: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """跑一个外部命令，输出按 UTF-8 收（Windows 上默认 GBK 会把中文日志弄乱码）。"""
    return subprocess.run(
        [tool, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def run_java(java: str, args: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return run(java, args, timeout=timeout)


def kotlin_home_bits() -> list[str]:
    return [str(p) for p in (COMPILER_JAR, STDLIB_JAR, COROUTINES_JAR, ANNOTATIONS_JAR)]


def download(url: str, dest: Path, *, label: str = "") -> None:
    """下载到 `dest`（幂等：已存在就跳过）。"""
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  下载 {label or dest.name} …", flush=True)
    with opener().open(url, timeout=300) as response, open(tmp, "wb") as handle:
        shutil.copyfileobj(response, handle, length=1 << 20)
    tmp.replace(dest)


def extract_android_jar() -> None:
    if ANDROID_JAR.exists():
        return
    assert PLATFORM_ZIP.exists(), f"缺少 {PLATFORM_ZIP}"
    with zipfile.ZipFile(PLATFORM_ZIP) as zf:
        member = next((n for n in zf.namelist() if n.endswith("android.jar")), None)
        assert member, "platform zip 里没有 android.jar"
        ANDROID_JAR.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(ANDROID_JAR, "wb") as dst:
            shutil.copyfileobj(src, dst)


def extract_build_tools() -> None:
    """解出 build-tools（剥掉 zip 顶层目录——它的名字是 `android-15` 这种，与版本无关）。"""
    if all((BUILD_TOOLS_HOME / rel).exists() for rel in BUILD_TOOLS_FILES.values()):
        return
    assert BUILD_TOOLS_ZIP.exists(), f"缺少 {BUILD_TOOLS_ZIP}"
    with zipfile.ZipFile(BUILD_TOOLS_ZIP) as zf:
        for info in zf.infolist():
            parts = info.filename.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            target = BUILD_TOOLS_HOME / parts[1]
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def build_tool(name: str) -> Path:
    """build-tools 里某个工具的路径；不在就报错（先调 `ensure_build_tools()`）。"""
    path = BUILD_TOOLS_HOME / BUILD_TOOLS_FILES[name]
    assert path.exists(), f"build-tools 缺少 {BUILD_TOOLS_FILES[name]}（跑 ensure_build_tools()）"
    return path


def missing_files(*, with_build_tools: bool = False) -> list[str]:
    """还缺哪些文件。

    `with_build_tools`：只有要**打 APK** 时才需要 aapt2 / d8 / zipalign / apksigner。
    真编译 + JVM 单测用不到它们，所以默认不把它们算成缺件——否则
    `verify_kotlin_compile.py` 会莫名其妙地开始要求下载 60MB。
    """
    missing = [str(path) for path in MAVEN_DOWNLOADS if not path.exists()]
    if not ANDROID_JAR.exists():
        missing.append(str(ANDROID_JAR))
    if with_build_tools and not all(
        (BUILD_TOOLS_HOME / rel).exists() for rel in BUILD_TOOLS_FILES.values()
    ):
        missing.append(str(BUILD_TOOLS_ZIP))
    return missing


def ensure_toolchain(*, with_build_tools: bool = False, download_missing: bool = True) -> list[str]:
    """把缺的工具链补齐（默认直接下载）。返回仍缺的文件（正常情况为空）。"""
    if download_missing:
        for path, url in MAVEN_DOWNLOADS.items():
            download(url, path, label=path.name)
        download(PLATFORM_URL, PLATFORM_ZIP, label="platform-35（约 64MB）")
        extract_android_jar()
        if with_build_tools:
            download(BUILD_TOOLS_URL, BUILD_TOOLS_ZIP, label="build-tools r35（约 60MB）")
            extract_build_tools()
    return missing_files(with_build_tools=with_build_tools)


def print_setup_hint(missing: list[str]) -> None:
    print("工具链不齐，缺这些文件：")
    for path in missing:
        print(f"  - {path}")
    proxy = proxy_url()
    flag = f'--proxy {proxy} ' if proxy else ""
    print("\n两个办法：① 去掉 `--no-download` 让脚本自己下；② 手工下：")
    print(f'  mkdir -p "{KOTLINC_DIR}"')
    for path, url in MAVEN_DOWNLOADS.items():
        print(f'  curl -L {flag}-o "{path}" {url}')
    print(f'  mkdir -p "{BUILD_TOOLS_DIR}"')
    print(f'  curl -L {flag}-o "{BUILD_TOOLS_ZIP}" {BUILD_TOOLS_URL}')
    print(f'  curl -L {flag}-o "{PLATFORM_ZIP}" {PLATFORM_URL}')
    print("  # 再跑一次本脚本，它会自动解出 android.jar 与 build-tools")


def banner(tool: str, version_args: list[str] | None = None) -> None:
    """打印一行工具版本，方便事后核对「这份 APK 是用什么打出来的」。"""
    proc = run(tool, version_args or ["--version"])
    line = (proc.stdout or proc.stderr).strip().splitlines()
    print(f"  {Path(tool).name}: {line[0] if line else '?'}")


# ---------------------------------------------------------------- 构建元数据
#
# 下面两个函数从 `build.gradle.kts` 与 `res/` **真解析**出名字，而不是手抄一份常量：
# 手抄的那份一定会在某次改资源/改版本号之后忘记同步，而症状是
# 「代码引用了不存在的资源」或「APK 里的版本号是旧的」——都不报错，只是错。


def resource_names() -> dict[str, set[str]]:
    """从 `res/` 解析真实的资源名（R 的依据）。"""
    names: dict[str, set[str]] = {"string": set(), "id": set(), "layout": set()}

    for values in (RES / "values").glob("*.xml"):
        for raw in re.findall(
            r'<(?:string|item)[^>]*name="([^"]+)"', values.read_text(encoding="utf-8")
        ):
            name = raw.replace(".", "_")
            # themes.xml 里的 `<item name="android:windowBackground">` 是样式项，不是资源名
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                names["string"].add(name)

    for layout in (RES / "layout").glob("*.xml"):
        names["layout"].add(layout.stem)
        names["id"].update(
            re.findall(r'android:id="@\+id/([A-Za-z0-9_]+)"', layout.read_text(encoding="utf-8"))
        )

    return names


def _one(text: str, pattern: str, what: str) -> str:
    match = re.search(pattern, text)
    assert match, f"在 android/app/build.gradle.kts 里找不到 {what}（{pattern}）"
    return match.group(1)


def gradle_app_config() -> dict[str, str]:
    """`build.gradle.kts` 里那些会进 BuildConfig 或者进清单的值。

    返回 `namespace` / `applicationId` / `version_code` / `version_name` /
    `min_sdk` / `target_sdk` / `build_config`（名字 → (Kotlin 类型, 字面量)）。
    """
    gradle = GRADLE_APP.read_text(encoding="utf-8")
    fields: dict[str, tuple[str, str]] = {
        "VERSION_CODE": ("int", _one(gradle, r"versionCode\s*=\s*(\d+)", "versionCode")),
        "VERSION_NAME": (
            "String",
            _one(gradle, r'versionName\s*=\s*("[^"]+")', "versionName"),
        ),
    }
    for kind, name, value in re.findall(
        r'buildConfigField\(\s*"(\w+)"\s*,\s*"(\w+)"\s*,\s*"([^"]+)"', gradle
    ):
        fields[name] = (kind, f'"{value}"' if kind == "String" else value)

    namespace = _one(gradle, r'namespace\s*=\s*"([^"]+)"', "namespace")
    return {
        "namespace": namespace,
        "application_id": _one(gradle, r'applicationId\s*=\s*"([^"]+)"', "applicationId"),
        "version_code": _one(gradle, r"versionCode\s*=\s*(\d+)", "versionCode"),
        "version_name": _one(gradle, r'versionName\s*=\s*"([^"]+)"', "versionName"),
        "min_sdk": _one(gradle, r"minSdk\s*=\s*(\d+)", "minSdk"),
        "target_sdk": _one(gradle, r"targetSdk\s*=\s*(\d+)", "targetSdk"),
        "build_config": fields,
    }


if __name__ == "__main__":  # pragma: no cover - 手工体检用
    missing = ensure_toolchain(download_missing="--no-download" not in sys.argv)
    if missing:
        print_setup_hint(missing)
        sys.exit(2)
    print("java        ", find_java())
    print("android.jar ", ANDROID_JAR)
    print("build-tools ", BUILD_TOOLS_HOME)
    banner(str(build_tool("aapt2")), ["version"])
