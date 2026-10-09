package tv.monet.app

import android.annotation.SuppressLint
import android.app.Activity
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import android.text.SpannableStringBuilder
import android.text.TextUtils
import android.text.style.ForegroundColorSpan
import android.text.style.StyleSpan
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.ImageView
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
 * Monet TV: a thin shell around the dashboard page (the same page as the Pi's kiosk,
 * served by the Pi), plus a native full-screen player for the box.
 *
 * Home screen: the dashboard in a WebView. The remote moves between cards inside the
 * page (instant, nothing goes over the network); OK on the live-preview card calls
 * MonetTV.openTv() and the player takes over.
 *
 * Full-screen TV: forwards the remote to the TVIP box through the Pi's key relay, and
 * picks the picture by what you're doing:
 *   NAV      while keys are pressed: the capture frames as motion JPEG (no encoder, ~0.2 s
 *            behind the box, no sound) so menus and channel changes respond at once.
 *   LIVE     SETTLE_MS after the last key: the 1080p RTSP stream (~1 s, with sound). It is
 *            prepared under the NAV picture and swapped in on its first frame (no black gap).
 *   SUBTITLE IDLE_MS after the last key, when the Pi has subtitles: HLS DELAY_MS behind
 *            live with Whisper subtitles. Any key goes straight back to NAV.
 *
 *   D-pad / OK / Back  -> box keys (short press)
 *   long-press Back    -> back to the dashboard
 *   long-press OK      -> channel picker (Soccer / Spanish / English shortlists)
 *
 * Whisper subtitles are matched to each HLS frame by its EXT-X-PROGRAM-DATE-TIME.
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
    private lateinit var dash: WebView
    private lateinit var navView: ImageView     // NAV picture (motion JPEG), above the player
    private var nav: MjpegReader? = null
    private var swapPending = false             // HD player prepared under NAV, waiting for its first frame
    private var inTv = false                  // false: dashboard on screen, player stopped
    private var lastKeySeen = 0L              // newest relay key time already reported
    // This TV's name (Settings > System > About > Device name), sent with box keys so the
    // other TVs sharing the box can say where a channel change came from.
    private val deviceName: String by lazy {
        Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME) ?: Build.MODEL
    }

    private enum class Mode { NAV, LIVE, SUBTITLE }
    private var mode = Mode.LIVE
    private var lastKeyAt = 0L
    private var visible = false
    private var captionsOn = false            // Pi has a language set and is keeping up
    private var subs: List<Sub> = emptyList()
    private val poller = Executors.newSingleThreadExecutor()
    private val tlWindow = Timeline.Window()

    /** gloss: lowercase word -> English meaning in this line (Gemini, via the Pi) */
    data class Sub(val start: Long, val end: Long, val text: String, val gloss: Map<String, String>)

    companion object {
        // Worst case for a line at the start of a chunk: 10 s chunk + up to ~6 s
        // Whisper + ~1 s Gemini = ~17 s. 10 s was too tight; 18 s keeps every line on time.
        const val DELAY_MS = 18_000L
        const val TAG = "MonetTV"
        const val IDLE_MS = DELAY_MS          // so the replay starts where the keys stopped
        const val SETTLE_MS = 4_000L          // no keys this long: NAV -> HD stream
    }

    private val ui = Handler(Looper.getMainLooper())
    private val net = Executors.newSingleThreadExecutor()   // keys go out in order
    private var retryMs = 2000L
    private var rtspFailures = 0          // after 3 in a row, fall back to HLS (4-8 s behind)
    private var onRtsp = false            // current source is RTSP (only its errors count above)

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
        navView = ImageView(this).apply {
            scaleType = ImageView.ScaleType.FIT_CENTER
            setBackgroundColor(Color.BLACK)
            visibility = View.GONE
        }
        root.addView(navView, FrameLayout.LayoutParams(-1, -1))

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
            setPadding(36, 14, 36, 16)
            background = pill(0xFFD6D6D6.toInt(), 16f)   // opaque light gray
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
        dash = dashboard()
        root.addView(dash, FrameLayout.LayoutParams(-1, -1))   // on top of the player
        setContentView(root)

        // Small buffers (~0.8 s): close to live, with enough slack for Wi-Fi jitter.
        player = ExoPlayer.Builder(this)
            .setLoadControl(DefaultLoadControl.Builder().setBufferDurationsMs(1000, 3000, 800, 1000).build())
            .build()
        view.player = player
        player.addListener(object : Player.Listener {
            override fun onPlayerError(error: PlaybackException) {
                if (onRtsp) rtspFailures++
                Log.w(TAG, "playback error (${if (onRtsp) "rtsp" else "hls"}, $mode, rtsp failures $rtspFailures): ${error.errorCodeName}")
                flash("Reconnecting…")
                ui.postDelayed({ play() }, retryMs)
                retryMs = (retryMs * 2).coerceAtMost(15000)
            }
            override fun onRenderedFirstFrame() {
                // the HD stream is on screen: drop the NAV picture
                if (swapPending) { swapPending = false; navView.visibility = View.GONE; stopNav() }
            }
            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_READY) { retryMs = 2000; if (onRtsp) rtspFailures = 0 }
                if (state == Player.STATE_ENDED) ui.postDelayed({ play() }, 1000)   // live stream "ended" = stall
            }
        })
        // play(), tick and poll start in onStart(): only while the app is on screen.
    }

    private fun useRtsp() = rtspFailures < 3

    // ------------------------------------------------------------------ dashboard (home)

    @SuppressLint("SetJavaScriptEnabled")
    private fun dashboard() = WebView(this).apply {
        setBackgroundColor(Color.BLACK)
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        // The page sizes everything off the screen width (rem = 100vw / 160): the 960-px-wide
        // TV viewport makes small labels ~5 px, and WebView's minimum font size / text zoom
        // would enlarge them and break the small tiles.
        settings.minimumFontSize = 1
        settings.minimumLogicalFontSize = 1
        settings.textZoom = 100
        addJavascriptInterface(Bridge(), "MonetTV")
        webViewClient = object : WebViewClient() {
            override fun onReceivedError(v: WebView, req: WebResourceRequest, err: WebResourceError) {
                if (req.isForMainFrame) ui.postDelayed({ if (!inTv) v.reload() }, 5000)   // Pi rebooting
            }
        }
        // The token logs this TV in once (cookie); the page drops it from the address.
        loadUrl("${BuildConfig.DASHBOARD_URL}/?t=${BuildConfig.RELAY_TOKEN}&tv=1")
    }

    /** Called by the page (window.MonetTV). */
    inner class Bridge {
        @JavascriptInterface fun openTv() { ui.post { enterTv() } }
        @JavascriptInterface fun device(): String = deviceName
    }

    private fun enterTv() {
        if (inTv) return
        inTv = true
        dash.evaluateJavascript("window.monetPreview && monetPreview(false)", null)   // stop its video stream
        dash.visibility = View.GONE
        dash.onPause(); dash.pauseTimers()
        lastKeyAt = System.currentTimeMillis()
        ensureStream()                  // HD stream for when we settle (NAV doesn't need it)
        startNav()
        ui.post(tick)
        ui.post(poll)
        flash("Hold  ↩  for the dashboard")
    }

    // ------------------------------------------------------------------ NAV picture (motion JPEG)

    /** Switch to the NAV picture. The player keeps its last frame on screen until the
     *  first motion-JPEG frame arrives, then stops (no stale image, no black flash). */
    private fun startNav() {
        mode = Mode.NAV
        swapPending = false
        if (nav?.isAlive == true) {             // already streaming (key during the HD swap)
            navView.visibility = View.VISIBLE
            player.stop()
            return
        }
        nav = MjpegReader { bmp ->
            ui.post {
                if (mode != Mode.NAV && !swapPending) return@post
                navView.setImageBitmap(bmp)
                if (navView.visibility != View.VISIBLE) { navView.visibility = View.VISIBLE; player.stop() }
            }
        }.also { it.start() }
    }

    private fun stopNav() { nav?.shutdown(); nav = null }

    /** Reads the dashboard's /api/frame.mjpg (multipart JPEG) and hands out decoded frames.
     *  960x540 at 20 fps: legible box menus, ~0.2 s behind the box. */
    private inner class MjpegReader(val onFrame: (Bitmap) -> Unit) : Thread("mjpeg") {
        @Volatile private var running = true
        @Volatile private var conn: HttpURLConnection? = null
        private val pool = arrayOfNulls<Bitmap>(3)   // reused bitmaps: no 2 MB allocation per frame
        private var slot = 0

        override fun run() {
            while (running) {
                try {
                    val c = URL("${BuildConfig.DASHBOARD_URL}/api/frame.mjpg?w=960&fps=20").openConnection() as HttpURLConnection
                    conn = c
                    c.connectTimeout = 3000
                    c.readTimeout = 5000
                    c.setRequestProperty("X-Token", BuildConfig.RELAY_TOKEN)
                    val input = java.io.BufferedInputStream(c.inputStream, 1 shl 16)
                    while (running) {
                        var len = -1
                        while (true) {                       // part headers
                            val line = readLine(input) ?: throw java.io.EOFException()
                            if (line.isEmpty()) { if (len >= 0) break else continue }
                            if (line.startsWith("Content-Length:", ignoreCase = true)) len = line.substring(15).trim().toInt()
                        }
                        val buf = ByteArray(len)
                        var off = 0
                        while (off < len) {
                            val n = input.read(buf, off, len - off)
                            if (n < 0) throw java.io.EOFException()
                            off += n
                        }
                        decode(buf, len)?.let { if (running) onFrame(it) }
                    }
                } catch (e: Exception) {
                    if (running) try { sleep(500) } catch (_: InterruptedException) { }
                } finally {
                    conn?.disconnect()
                }
            }
        }

        private fun decode(buf: ByteArray, len: Int): Bitmap? {
            slot = (slot + 1) % pool.size
            val opts = BitmapFactory.Options().apply { inMutable = true; inBitmap = pool[slot] }
            val bmp = try { BitmapFactory.decodeByteArray(buf, 0, len, opts) }
                      catch (e: IllegalArgumentException) { BitmapFactory.decodeByteArray(buf, 0, len, BitmapFactory.Options().apply { inMutable = true }) }
            pool[slot] = bmp
            return bmp
        }

        private fun readLine(i: java.io.InputStream): String? {
            val sb = StringBuilder()
            while (true) {
                val b = i.read()
                if (b < 0) return null
                if (b == '\n'.code) return sb.toString().trimEnd('\r')
                sb.append(b.toChar())
            }
        }

        fun shutdown() {
            running = false
            try { conn?.disconnect() } catch (_: Exception) { }
            interrupt()
        }
    }

    /** The Pi only encodes the TV stream while someone watches (it stops it after a few
     *  idle minutes); ask for it whenever full screen starts. A no-op if it's already on;
     *  if it was off the hub needs ~3 s, which the player's retry covers. */
    private fun ensureStream() {
        net.execute { try { http("POST", "tv/cast/on") } catch (e: Exception) { } }
    }

    private fun leaveTv() {
        inTv = false
        ui.removeCallbacks(tick); ui.removeCallbacks(poll)   // no caption polling: no Gemini spend
        stopNav(); swapPending = false; navView.visibility = View.GONE
        player.stop()
        hidePicker()
        caption.visibility = View.GONE; shownSub = null
        dash.visibility = View.VISIBLE
        dash.onResume(); dash.resumeTimers()
        dash.evaluateJavascript("window.monetPreview && monetPreview(true)", null)
        dash.requestFocus()
    }

    // ------------------------------------------------------------------ modes + subtitles

    private val tick = object : Runnable {
        override fun run() {
            val idle = System.currentTimeMillis() - lastKeyAt
            val calm = picker.visibility != View.VISIBLE
            if (mode == Mode.NAV && calm && idle >= SETTLE_MS) {
                mode = Mode.LIVE
                rtspFailures = 0
                swapPending = true          // keep showing NAV frames until the HD stream's first frame
                Log.i(TAG, "settled: nav -> HD live")
                play()
            }
            if (mode == Mode.LIVE && captionsOn && idle >= IDLE_MS && calm) {
                mode = Mode.SUBTITLE
                Log.i(TAG, "idle: live -> CC")
                play()
            }
            if (mode == Mode.SUBTITLE && !captionsOn) { mode = Mode.LIVE; play() }
            showSubtitle()
            modeChip.text = when (mode) { Mode.NAV -> "● LIVE"; Mode.LIVE -> "● LIVE  HD"; else -> "CC  −${DELAY_MS / 1000}s" }
            modeChip.background = pill(if (mode == Mode.SUBTITLE) 0x992E7DE6.toInt() else 0x99E0245A.toInt(), 30f)
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
                    val lk = o.optJSONObject("last_key")
                    val by = lk?.optString("device").orEmpty()
                    val at = lk?.optLong("at") ?: 0L
                    ui.post {
                        subs = list; captionsOn = on
                        // the box is shared: say so when another TV changed something
                        if (at > lastKeySeen) {
                            if (lastKeySeen > 0 && by.isNotEmpty() && by != deviceName) flash("Changed from $by")
                            lastKeySeen = at
                        }
                    }
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

    /**
     * Up to four lines: each Spanish line has its own English line directly above it,
     * holding the glosses for that line's key words in order. Key words are highlighted
     * in the Spanish line.
     */
    private fun renderSubtitle(s: Sub) {
        caption.removeAllViews()
        val strip = Regex("^[¿¡\"«(]+|[.,;:!?\"»)…]+$")
        val face = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        for (line in s.text.split("\n").filter { it.isNotBlank() }) {
            val spanish = SpannableStringBuilder()
            val english = mutableListOf<String>()
            for (word in line.split(" ").filter { it.isNotBlank() }) {
                if (spanish.isNotEmpty()) spanish.append(" ")
                val gloss = s.gloss[word.replace(strip, "").lowercase()]
                val at = spanish.length
                spanish.append(word)
                if (gloss != null) {
                    english += gloss
                    spanish.setSpan(ForegroundColorSpan(0xFFB3261E.toInt()), at, spanish.length, 0)
                    spanish.setSpan(StyleSpan(Typeface.BOLD), at, spanish.length, 0)
                }
            }
            caption.addView(TextView(this).apply {
                text = english.joinToString("  ·  ")
                textSize = 20f
                setTextColor(0xFF0B5C55.toInt())
                typeface = face
                gravity = Gravity.CENTER
                maxLines = 1
                ellipsize = TextUtils.TruncateAt.END
                visibility = if (english.isEmpty()) View.INVISIBLE else View.VISIBLE   // keeps four rows steady
            })
            caption.addView(TextView(this).apply {
                text = spanish
                textSize = 30f
                setTextColor(0xFF111111.toInt())
                typeface = face
                gravity = Gravity.CENTER
                maxLines = 1
            })
        }
    }

    /** Remote activity: make the box's response visible right away. */
    private fun keyActivity(toBox: Boolean) {
        lastKeyAt = System.currentTimeMillis()
        if (toBox && mode != Mode.NAV) {
            Log.i(TAG, "key: $mode -> nav")
            startNav()
        }
    }

    private fun play() {
        if (!visible || !inTv || mode == Mode.NAV) return   // off screen, or NAV (motion JPEG, no player)
        onRtsp = mode == Mode.LIVE && useRtsp()
        Log.i(TAG, "play: $mode via ${if (onRtsp) "RTSP" else "HLS"}")
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

    // In the background the app must go fully quiet: stop the stream and every timer.
    // (Before: onStop only paused, and a retry / CC switch / stall restart called play()
    // again, so a hidden copy kept playing under the visible one: two soundtracks.)
    override fun onStop() {
        super.onStop()
        visible = false
        ui.removeCallbacksAndMessages(null)
        stopNav(); swapPending = false
        player.stop()
        dash.onPause(); dash.pauseTimers()
    }

    override fun onStart() {
        super.onStart()
        visible = true
        if (inTv) {
            lastKeyAt = System.currentTimeMillis()
            ensureStream()
            startNav()
            ui.post(tick)
            ui.post(poll)
        } else {
            dash.onResume(); dash.resumeTimers()
            dash.requestFocus()
        }
    }

    override fun onDestroy() {
        ui.removeCallbacksAndMessages(null)
        dash.destroy()
        player.release()
        stopNav()
        net.shutdownNow()
        poller.shutdownNow()
        super.onDestroy()
    }

    // ------------------------------------------------------------------ keys

    // On the dashboard the WebView gets the keys; what it doesn't use (Back) gets the
    // default handling (Back leaves the app).
    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        if (!inTv) return super.onKeyDown(keyCode, event)
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
        if (!inTv) return super.onKeyLongPress(keyCode, event)
        longPressed = true
        when (keyCode) {
            KeyEvent.KEYCODE_BACK -> if (picker.visibility == View.VISIBLE) hidePicker() else leaveTv()
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER -> showPicker()
        }
        return true
    }

    override fun onKeyUp(keyCode: Int, event: KeyEvent): Boolean {
        if (!inTv) return super.onKeyUp(keyCode, event)
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
        conn.setRequestProperty("X-Device", deviceName)
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
