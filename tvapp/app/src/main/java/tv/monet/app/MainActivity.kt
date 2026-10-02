package tv.monet.app

import android.app.Activity
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.hls.HlsMediaSource
import androidx.media3.exoplayer.rtsp.RtspMediaSource
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * Monet TV: plays the Pi's HDMI-capture stream full screen and forwards the
 * Chromecast remote to the TVIP box through the Pi's key relay.
 *
 *   D-pad / OK / Back  -> box keys (short press)
 *   long-press Back    -> exit the app
 *   long-press OK      -> channel picker (Soccer / Spanish / English shortlists)
 */
class MainActivity : Activity() {

    private lateinit var player: ExoPlayer
    private lateinit var status: TextView
    private lateinit var picker: LinearLayout
    private lateinit var pickerTitle: TextView
    private lateinit var pickerList: LinearLayout
    private lateinit var pickerScroll: ScrollView

    private val ui = Handler(Looper.getMainLooper())
    private val net = Executors.newSingleThreadExecutor()   // keys go out in order
    private var retryMs = 2000L
    private var rtspFailures = 0          // after 3 in a row, fall back to HLS (4-8 s behind)

    private var lists: List<Pair<String, List<Channel>>> = emptyList()
    private var tab = 0
    private var row = 0
    private var longPressed = false

    data class Channel(val number: Int, val name: String, val now: String)

    private val LABELS = mapOf(
        "up" to "↑", "down" to "↓", "left" to "←", "right" to "→",
        "ch_up" to "CH +", "ch_down" to "CH −", "guide" to "Guide", "play_pause" to "Info")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        val root = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
        val view = PlayerView(this).apply {
            useController = false
            resizeMode = AspectRatioFrameLayout.RESIZE_MODE_FIT
            isFocusable = false
        }
        root.addView(view)

        status = TextView(this).apply {
            setTextColor(Color.WHITE)
            textSize = 20f
            typeface = Typeface.DEFAULT_BOLD
            setPadding(28, 14, 28, 14)
            background = pill(0xCC000000.toInt())
            visibility = View.GONE
        }
        root.addView(status, FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.END).apply {
            setMargins(0, 40, 48, 0)
        })

        picker = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(36, 32, 36, 32)
            background = pill(0xE6101018.toInt(), 28f)
            visibility = View.GONE
        }
        pickerTitle = TextView(this).apply {
            setTextColor(Color.WHITE); textSize = 24f; typeface = Typeface.DEFAULT_BOLD
            setPadding(8, 0, 8, 20)
        }
        pickerList = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        pickerScroll = ScrollView(this).apply { addView(pickerList); isFocusable = false }
        picker.addView(pickerTitle)
        picker.addView(pickerScroll, LinearLayout.LayoutParams(-1, 0, 1f))
        root.addView(picker, FrameLayout.LayoutParams(760, -1, Gravity.START).apply {
            setMargins(48, 48, 0, 48)
        })
        setContentView(root)

        // Tiny buffers: play as close to live as possible (RTSP is ~0.5-1.5 s behind the box).
        player = ExoPlayer.Builder(this)
            .setLoadControl(DefaultLoadControl.Builder().setBufferDurationsMs(300, 1500, 150, 300).build())
            .build()
        view.player = player
        player.addListener(object : Player.Listener {
            override fun onPlayerError(error: PlaybackException) {
                rtspFailures++
                flash(if (useRtsp()) "Reconnecting…" else "Reconnecting (HLS)…")
                ui.postDelayed({ play() }, retryMs)
                retryMs = (retryMs * 2).coerceAtMost(15000)
            }
            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_READY) { retryMs = 2000; if (useRtsp()) rtspFailures = 0 }
                if (state == Player.STATE_ENDED) ui.postDelayed({ play() }, 1000)   // live stream "ended" = stall
            }
        })
        play()
    }

    private fun useRtsp() = rtspFailures < 3

    private fun play() {
        if (useRtsp()) {
            // RTSP over TCP straight from MediaMTX: no segmenting, so ~0.5-1.5 s of lag.
            val src = RtspMediaSource.Factory().setForceUseRtpTcp(true).setTimeoutMs(4000)
                .createMediaSource(MediaItem.fromUri(BuildConfig.RTSP_URL))
            player.setMediaSource(src)
        } else {
            val item = MediaItem.Builder()
                .setUri(BuildConfig.STREAM_URL)
                .setLiveConfiguration(MediaItem.LiveConfiguration.Builder().setTargetOffsetMs(2000).build())
                .build()
            player.setMediaSource(HlsMediaSource.Factory(DefaultHttpDataSource.Factory()).createMediaSource(item))
        }
        player.prepare()
        player.playWhenReady = true
    }

    override fun onStop() {
        super.onStop()
        player.pause()
    }

    override fun onStart() {
        super.onStart()
        if (::player.isInitialized) { player.seekToDefaultPosition(); player.play() }
    }

    override fun onDestroy() {
        player.release()
        net.shutdownNow()
        super.onDestroy()
    }

    // ------------------------------------------------------------------ keys

    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        if (keyCode == KeyEvent.KEYCODE_BACK || keyCode == KeyEvent.KEYCODE_DPAD_CENTER ||
            keyCode == KeyEvent.KEYCODE_ENTER) {
            if (event.repeatCount == 0) { event.startTracking(); longPressed = false }
            return true
        }
        if (picker.visibility == View.VISIBLE) return pickerKey(keyCode)
        val box = when (keyCode) {
            KeyEvent.KEYCODE_DPAD_UP -> "up"
            KeyEvent.KEYCODE_DPAD_DOWN -> "down"
            KeyEvent.KEYCODE_DPAD_LEFT -> "left"
            KeyEvent.KEYCODE_DPAD_RIGHT -> "right"
            KeyEvent.KEYCODE_CHANNEL_UP -> "ch_up"
            KeyEvent.KEYCODE_CHANNEL_DOWN -> "ch_down"
            KeyEvent.KEYCODE_GUIDE -> "guide"
            KeyEvent.KEYCODE_INFO, KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE -> "play_pause"
            in KeyEvent.KEYCODE_0..KeyEvent.KEYCODE_9 -> (keyCode - KeyEvent.KEYCODE_0).toString()
            else -> return super.onKeyDown(keyCode, event)
        }
        send("key/$box", LABELS[box] ?: box)
        return true
    }

    override fun onKeyLongPress(keyCode: Int, event: KeyEvent): Boolean {
        longPressed = true
        when (keyCode) {
            KeyEvent.KEYCODE_BACK -> if (picker.visibility == View.VISIBLE) hidePicker() else finish()
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER -> showPicker()
        }
        return true
    }

    override fun onKeyUp(keyCode: Int, event: KeyEvent): Boolean {
        val short = !longPressed && event.isTracking && !event.isCanceled
        when (keyCode) {
            KeyEvent.KEYCODE_BACK -> {
                if (short) { if (picker.visibility == View.VISIBLE) hidePicker() else send("key/back", "↩  Back") }
                return true
            }
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER -> {
                if (short) { if (picker.visibility == View.VISIBLE) pickerChoose() else send("key/ok", "OK") }
                return true
            }
        }
        return super.onKeyUp(keyCode, event)
    }

    private fun send(path: String, label: String) {
        flash(label)
        net.execute {
            val ok = try { http("POST", path) != null } catch (e: Exception) { false }
            if (!ok) ui.post { flash("Box not reachable") }
        }
    }

    // ------------------------------------------------------------------ channel picker

    private fun showPicker() {
        picker.visibility = View.VISIBLE
        pickerTitle.text = "Loading channels…"
        net.execute {
            val parsed = try {
                val o = JSONObject(http("GET", "channels") ?: "{}")
                listOf("soccer", "spanish", "english").filter { o.has(it) }.map { k ->
                    val l = o.getJSONObject(k)
                    val arr = l.getJSONArray("channels")
                    l.getString("title") to (0 until arr.length()).map {
                        val c = arr.getJSONObject(it)
                        Channel(c.getInt("number"), c.getString("name"), c.optString("now"))
                    }
                }
            } catch (e: Exception) { emptyList() }
            ui.post {
                lists = parsed; tab = 0; row = 0
                if (lists.isEmpty()) pickerTitle.text = "Channel list unavailable" else renderPicker()
            }
        }
    }

    private fun hidePicker() { picker.visibility = View.GONE }

    private fun pickerKey(keyCode: Int): Boolean {
        if (lists.isEmpty()) return true
        val n = lists[tab].second.size
        when (keyCode) {
            KeyEvent.KEYCODE_DPAD_UP -> row = (row - 1 + n) % n
            KeyEvent.KEYCODE_DPAD_DOWN -> row = (row + 1) % n
            KeyEvent.KEYCODE_DPAD_LEFT -> { tab = (tab - 1 + lists.size) % lists.size; row = 0 }
            KeyEvent.KEYCODE_DPAD_RIGHT -> { tab = (tab + 1) % lists.size; row = 0 }
            else -> return true
        }
        renderPicker()
        return true
    }

    private fun renderPicker() {
        val (title, chans) = lists[tab]
        pickerTitle.text = lists.mapIndexed { i, l -> if (i == tab) "[ ${l.first} ]" else l.first }
            .joinToString("   ") + "    ◀ ▶"
        pickerList.removeAllViews()
        chans.forEachIndexed { i, c ->
            val tv = TextView(this).apply {
                text = "${c.number}   ${c.name}" + if (c.now.isNotEmpty()) "\n      ${c.now}" else ""
                textSize = 18f
                setPadding(16, 10, 16, 10)
                setTextColor(if (i == row) Color.BLACK else Color.WHITE)
                background = if (i == row) pill(Color.WHITE, 14f) else null
            }
            pickerList.addView(tv)
        }
        pickerList.post {
            val child = pickerList.getChildAt(row) ?: return@post
            pickerScroll.smoothScrollTo(0, (child.top - pickerScroll.height / 3).coerceAtLeast(0))
        }
    }

    private fun pickerChoose() {
        if (lists.isEmpty()) return
        val c = lists[tab].second[row]
        hidePicker()
        send("tune/${c.number}", "Tuning ${c.number}  ${c.name}")
    }

    // ------------------------------------------------------------------ helpers

    private fun http(method: String, path: String): String? {
        val conn = URL("${BuildConfig.RELAY_URL}/$path").openConnection() as HttpURLConnection
        conn.requestMethod = method
        conn.connectTimeout = 3000
        conn.readTimeout = 10000
        conn.setRequestProperty("X-Token", BuildConfig.RELAY_TOKEN)
        if (method == "POST") { conn.doOutput = true; conn.outputStream.close() }
        return try {
            if (conn.responseCode in 200..299) conn.inputStream.bufferedReader().readText() else null
        } finally { conn.disconnect() }
    }

    private val hideStatus = Runnable { status.visibility = View.GONE }

    private fun flash(text: String) {
        status.text = text
        status.visibility = View.VISIBLE
        ui.removeCallbacks(hideStatus)
        ui.postDelayed(hideStatus, 1500)
    }

    private fun pill(color: Int, radius: Float = 40f) = GradientDrawable().apply {
        setColor(color); cornerRadius = radius
    }
}
