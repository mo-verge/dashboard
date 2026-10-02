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
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.Timeline
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
 *
 * Two modes:
 *   LIVE     RTSP, ~1 s behind the box, no subtitles. Any remote key switches here
 *            at once so the box's response is visible.
 *   SUBTITLE after IDLE_MS without keys (and when the Pi has subtitles in a
 *            language), HLS played DELAY_MS behind live; Whisper subtitles from
 *            the Pi are matched to each frame by its EXT-X-PROGRAM-DATE-TIME.
 */
class MainActivity : Activity() {

    private lateinit var player: ExoPlayer
    private lateinit var status: TextView
    private lateinit var picker: LinearLayout
    private lateinit var pickerTitle: TextView
    private lateinit var pickerList: LinearLayout
    private lateinit var pickerScroll: ScrollView
    private lateinit var caption: LinearLayout
    private var shownSub: Sub? = null
    private lateinit var modeChip: TextView

    private enum class Mode { LIVE, SUBTITLE }
    private var mode = Mode.LIVE
    private var lastKeyAt = 0L
    private var captionsOn = false            // Pi has a language set and is keeping up
    private var subs: List<Sub> = emptyList()
    private val poller = Executors.newSingleThreadExecutor()
    private val tlWindow = Timeline.Window()

    /** gloss: lowercase word -> English meaning in this line (Gemini, via the Pi) */
    data class Sub(val start: Long, val end: Long, val text: String, val gloss: Map<String, String>)

    companion object {
        const val DELAY_MS = 10_000L          // subtitles + glosses are ready ~6 s after speech
        const val IDLE_MS = DELAY_MS          // so the replay starts where the keys stopped
    }

    private val ui = Handler(Looper.getMainLooper())
    private val net = Executors.newSingleThreadExecutor()   // keys go out in order
    private var retryMs = 2000L
    private var rtspFailures = 0          // after 3 in a row, fall back to HLS (4-8 s behind)

    private var lists: List<Pair<String, List<Channel>>> = emptyList()
    private var tab = 0
    private var row = 0
    private var longPressed = false

    data class Channel(val number: Int, val name: String, val now: String, val lang: String)

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
        caption = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(30, 8, 30, 14)
            background = pill(0xB3000000.toInt(), 16f)
            visibility = View.GONE
        }
        root.addView(caption, FrameLayout.LayoutParams(-2, -2, Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL).apply {
            setMargins(80, 0, 80, 70)
        })
        modeChip = TextView(this).apply {
            setTextColor(Color.WHITE); textSize = 15f; typeface = Typeface.DEFAULT_BOLD
            setPadding(20, 8, 20, 8)
            alpha = 0.8f
        }
        root.addView(modeChip, FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.START).apply {
            setMargins(48, 40, 0, 0)
        })
        setContentView(root)

        // Small buffers (~0.8 s): close to live, with enough slack for Wi-Fi jitter.
        player = ExoPlayer.Builder(this)
            .setLoadControl(DefaultLoadControl.Builder().setBufferDurationsMs(1000, 3000, 800, 1000).build())
            .build()
        view.player = player
        player.addListener(object : Player.Listener {
            override fun onPlayerError(error: PlaybackException) {
                if (mode == Mode.LIVE) rtspFailures++
                flash("Reconnecting…")
                ui.postDelayed({ play() }, retryMs)
                retryMs = (retryMs * 2).coerceAtMost(15000)
            }
            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_READY) { retryMs = 2000; if (mode == Mode.LIVE && useRtsp()) rtspFailures = 0 }
                if (state == Player.STATE_ENDED) ui.postDelayed({ play() }, 1000)   // live stream "ended" = stall
            }
        })
        lastKeyAt = System.currentTimeMillis()
        play()
        ui.post(tick)
        ui.post(poll)
    }

    private fun useRtsp() = rtspFailures < 3

    // ------------------------------------------------------------------ modes + subtitles

    private val tick = object : Runnable {
        override fun run() {
            val idle = System.currentTimeMillis() - lastKeyAt
            if (mode == Mode.LIVE && captionsOn && idle >= IDLE_MS && picker.visibility != View.VISIBLE) {
                mode = Mode.SUBTITLE
                play()
            }
            if (mode == Mode.SUBTITLE && !captionsOn) { mode = Mode.LIVE; play() }
            showSubtitle()
            modeChip.text = if (mode == Mode.LIVE) "● LIVE" else "CC  −${DELAY_MS / 1000}s"
            modeChip.background = pill(if (mode == Mode.LIVE) 0x99E0245A.toInt() else 0x992E7DE6.toInt(), 30f)
            ui.postDelayed(this, 200)
        }
    }

    private val poll = object : Runnable {
        override fun run() {
            poller.execute {
                try {
                    val since = System.currentTimeMillis() - 90_000
                    val o = JSONObject(http("GET", "captions?since=$since") ?: "{}")
                    val arr = o.optJSONArray("captions")
                    val list = (0 until (arr?.length() ?: 0)).map {
                        val c = arr!!.getJSONObject(it)
                        val g = c.optJSONArray("gloss")
                        val gloss = (0 until (g?.length() ?: 0)).associate { k ->
                            val o2 = g!!.getJSONObject(k)
                            o2.getString("w").lowercase() to o2.getString("en")
                        }
                        Sub(c.getLong("start"), c.getLong("end"), c.getString("text"), gloss)
                    }
                    // subtitles only make sense if the Pi is transcribing and keeping up
                    val on = o.optString("lang") != "off" && o.optLong("now") - o.optLong("ready_until") < 30_000
                    ui.post { subs = list; captionsOn = on }
                } catch (e: Exception) {
                    ui.post { captionsOn = false }
                }
            }
            ui.postDelayed(this, 2000)
        }
    }

    /** Wall-clock time of the frame on screen (HLS: from EXT-X-PROGRAM-DATE-TIME). */
    private fun frameWallMs(): Long? {
        val tl = player.currentTimeline
        if (tl.isEmpty) return null
        tl.getWindow(player.currentMediaItemIndex, tlWindow)
        if (tlWindow.windowStartTimeMs == C.TIME_UNSET) return null
        return tlWindow.windowStartTimeMs + player.currentPosition
    }

    private fun showSubtitle() {
        val t = if (mode == Mode.SUBTITLE) frameWallMs() else null
        val s = t?.let { now -> subs.lastOrNull { it.start <= now && now <= it.end } }
        if (s == null) { caption.visibility = View.GONE; shownSub = null; return }
        if (s != shownSub) { renderSubtitle(s); shownSub = s }   // data class: re-render when glosses arrive
        caption.visibility = View.VISIBLE
    }

    /** Subtitle line(s) as word columns; key words get their English in small teal above. */
    private fun renderSubtitle(s: Sub) {
        caption.removeAllViews()
        val strip = Regex("^[¿¡\"«(]+|[.,;:!?\"»)…]+$")
        for (line in s.text.split("\n")) {
            val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.BOTTOM }
            for (word in line.split(" ").filter { it.isNotBlank() }) {
                val gloss = s.gloss[word.replace(strip, "").lowercase()]
                val col = LinearLayout(this).apply {
                    orientation = LinearLayout.VERTICAL
                    gravity = Gravity.CENTER_HORIZONTAL
                    setPadding(9, 0, 9, 0)
                }
                col.addView(TextView(this).apply {
                    text = gloss ?: ""
                    textSize = 16f
                    setTextColor(0xFF2EE6D6.toInt())
                    typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
                    gravity = Gravity.CENTER
                    maxLines = 1
                })
                col.addView(TextView(this).apply {
                    text = word
                    textSize = 30f
                    setTextColor(if (gloss != null) 0xFFFFE9A8.toInt() else Color.WHITE)
                    typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
                    gravity = Gravity.CENTER
                })
                row.addView(col)
            }
            caption.addView(row)
        }
    }

    /** Remote activity: make the box's response visible right away. */
    private fun keyActivity(toBox: Boolean) {
        lastKeyAt = System.currentTimeMillis()
        if (toBox && mode == Mode.SUBTITLE) { mode = Mode.LIVE; play() }
    }

    private fun play() {
        if (mode == Mode.SUBTITLE) {
            // ~DELAY_MS behind live: room for Whisper, and the replay starts at the last key press
            val item = MediaItem.Builder()
                .setUri(BuildConfig.STREAM_URL)
                .setLiveConfiguration(MediaItem.LiveConfiguration.Builder()
                    .setTargetOffsetMs(DELAY_MS).setMinOffsetMs(DELAY_MS - 3000).setMaxOffsetMs(DELAY_MS + 6000)
                    .build())
                .build()
            player.setMediaSource(HlsMediaSource.Factory(DefaultHttpDataSource.Factory()).createMediaSource(item))
        } else if (useRtsp()) {
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
        ui.removeCallbacksAndMessages(null)
        player.release()
        net.shutdownNow()
        poller.shutdownNow()
        super.onDestroy()
    }

    // ------------------------------------------------------------------ keys

    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        if (keyCode == KeyEvent.KEYCODE_BACK || keyCode == KeyEvent.KEYCODE_DPAD_CENTER ||
            keyCode == KeyEvent.KEYCODE_ENTER) {
            if (event.repeatCount == 0) { event.startTracking(); longPressed = false }
            return true
        }
        if (picker.visibility == View.VISIBLE) { keyActivity(false); return pickerKey(keyCode) }
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
        keyActivity(true)
        flash(label)
        net.execute {
            val ok = try { http("POST", path) != null } catch (e: Exception) { false }
            if (!ok) ui.post { flash("Box not reachable") }
        }
    }

    // ------------------------------------------------------------------ channel picker

    private fun showPicker() {
        keyActivity(false)
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
                        Channel(c.getInt("number"), c.getString("name"), c.optString("now"), c.optString("lang"))
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
        // subtitle language follows the channel (inventory): es / en, anything else off
        val lang = if (c.lang == "es" || c.lang == "en") c.lang else "off"
        net.execute { try { http("POST", "caption-lang/$lang") } catch (e: Exception) { } }
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
