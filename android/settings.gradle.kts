pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // 路线 A（Chaquopy）才需要，见顶部 build.gradle.kts 的说明：
        // maven("https://chaquo.com/maven")
    }
}

rootProject.name = "shadow-android"
include(":app")
