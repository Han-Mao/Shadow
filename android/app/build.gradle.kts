plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.bluewhale.shadow"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.bluewhale.shadow"
        // minSdk 26：Android 8。
        // 为什么不是 24（Chaquopy 的下限、dispatchGesture 的下限）：26 才有
        // NotificationChannel，而设备端点要靠前台服务常驻，把版本分支砍掉能让
        // 这段代码真的被测到，而不是写两条只跑其中一条的路径。
        minSdk = 26
        // targetSdk 34 而不是 35：35 起前台服务类型与部分权限的收紧行为还没在这个
        // 应用上验证过。等 34 跑稳再抬——部署类改动不该一次性引入两个变量。
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
        // 端点端口。可用 `adb shell am start ... --ei port` 之外的方式改，
        // 正式做法见 DeviceEndpointService 的 EXTRA_PORT。
        buildConfigField("int", "DEFAULT_ENDPOINT_PORT", "8765")
    }

    buildFeatures {
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    // ---- 本地 JVM 单测 ----
    // UiTreeSerializer 被刻意设计成**不碰 Android 框架类**（它只看 UiNodeAdapter），
    // 所以它能在普通 JVM 上跑 `./gradlew :app:test` ——不需要真机、不需要模拟器。
    // 这是「UI 树格式必须与 uiautomator 兼容」这条约定唯一能被自动守住的地方：
    // Python 侧的 pytest 只能验证「满足约定的 XML 能被解析」，无法验证这个序列化器
    // 真的吐出了那种 XML。
    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    // 运行时零第三方依赖是刻意的：
    //   - HTTP 端点用 ServerSocket（JDK 自带）
    //   - JSON 用 org.json（Android 平台自带）
    //   - 权限/设置跳转用平台 Intent
    // 依赖越少，能在越老的机器上构建，出问题的变量也越少。
    testImplementation("junit:junit:4.13.2")
}
