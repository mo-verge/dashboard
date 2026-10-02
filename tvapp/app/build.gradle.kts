import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Stream / relay settings live outside git: tvapp/monet.properties
//   rtspUrl=rtsp://192.168.50.158:8554/tv            (low latency, used first)
//   streamUrl=http://192.168.50.158:8888/tv/index.m3u8  (HLS fallback)
//   relayUrl=http://192.168.50.158:8180
//   relayToken=<contents of ~/.config/dashboard/relay-token on the Pi>
val monet = Properties().apply {
    val f = rootProject.file("monet.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
fun cfg(key: String, default: String) = "\"" + (monet.getProperty(key) ?: default) + "\""

android {
    namespace = "tv.monet.app"
    compileSdk = 35
    defaultConfig {
        applicationId = "tv.monet.app"
        minSdk = 26
        targetSdk = 34
        versionCode = 3
        versionName = "1.2"
        buildConfigField("String", "RTSP_URL", cfg("rtspUrl", "rtsp://192.168.50.158:8554/tv"))
        buildConfigField("String", "STREAM_URL", cfg("streamUrl", "http://192.168.50.158:8888/tv/index.m3u8"))
        buildConfigField("String", "RELAY_URL", cfg("relayUrl", "http://192.168.50.158:8180"))
        buildConfigField("String", "RELAY_TOKEN", cfg("relayToken", ""))
    }
    buildFeatures { buildConfig = true }
    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")   // sideloaded, not published
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    val media3 = "1.4.1"
    implementation("androidx.media3:media3-exoplayer:$media3")
    implementation("androidx.media3:media3-exoplayer-hls:$media3")
    implementation("androidx.media3:media3-exoplayer-rtsp:$media3")
    implementation("androidx.media3:media3-ui:$media3")
    implementation("androidx.appcompat:appcompat:1.7.0")
}
