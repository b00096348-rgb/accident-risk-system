package ae.aus.roadsense

import android.Manifest
import android.annotation.SuppressLint
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity

/**
 * Hosts the web interface and wires it to the native capture service.
 *
 * The driver sees the same screens as the browser build. What the browser
 * cannot do is talk to an ELM327 over Bluetooth Classic, so the page tells this
 * activity when a session starts and the native service takes over capture.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var settings: SettingsStore
    private lateinit var web: WebView

    private val permissions = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { granted ->
        val missing = granted.filterValues { !it }.keys
        if (missing.isNotEmpty()) {
            Toast.makeText(
                this,
                "Recording needs Bluetooth and location. Grant them in Settings.",
                Toast.LENGTH_LONG
            ).show()
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = SettingsStore(this)

        web = WebView(this)
        setContentView(web)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true      // the page keeps its token here
        web.settings.databaseEnabled = true
        web.webViewClient = object : WebViewClient() {
            override fun onReceivedError(
                view: WebView?, request: android.webkit.WebResourceRequest?,
                error: android.webkit.WebResourceError?,
            ) {
                if (request?.isForMainFrame == true) showServerDialog(unreachable = true)
            }
        }
        web.addJavascriptInterface(Bridge(), "RoadSenseNative")

        requestRuntimePermissions()

        if (settings.serverUrl.isBlank()) showServerDialog() else load()
    }

    private fun load() {
        web.loadUrl("${settings.serverUrl}/app")
    }

    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else super.onBackPressed()
    }

    private fun requestRuntimePermissions() {
        val wanted = mutableListOf(
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION,
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            wanted += Manifest.permission.BLUETOOTH_CONNECT
            wanted += Manifest.permission.BLUETOOTH_SCAN
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            wanted += Manifest.permission.POST_NOTIFICATIONS
        }
        permissions.launch(wanted.toTypedArray())
    }

    /** Ask for the server address. Shown on first run and whenever it fails. */
    private fun showServerDialog(unreachable: Boolean = false) {
        val field = EditText(this).apply {
            hint = "http://192.168.1.20:8000"
            setText(settings.serverUrl)
        }
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 24, 48, 0)
            addView(field)
        }
        AlertDialog.Builder(this)
            .setTitle(if (unreachable) "Cannot reach the server" else "Server address")
            .setMessage(
                if (unreachable) {
                    "Check the address and that the server is running on the same network."
                } else {
                    "Where the RoadSense server is running."
                }
            )
            .setView(layout)
            .setPositiveButton("Save") { _, _ ->
                settings.serverUrl = field.text.toString()
                load()
            }
            .setNeutralButton("App settings") { _, _ ->
                startActivity(
                    Intent(
                        Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.fromParts("package", packageName, null)
                    )
                )
            }
            .setCancelable(false)
            .show()
    }

    /**
     * Exposed to the page as `window.RoadSenseNative`.
     *
     * The web app calls these when a session begins and ends. Running in a
     * normal browser the object is simply absent and the page skips the calls.
     */
    inner class Bridge {

        @JavascriptInterface
        fun sessionStarted(streamId: String) {
            if (streamId.isBlank()) return
            runOnUiThread { CaptureService.start(this@MainActivity, streamId) }
        }

        @JavascriptInterface
        fun sessionEnded() {
            runOnUiThread { CaptureService.stop(this@MainActivity) }
        }

        /** Lets the page show a "change server" affordance rather than hiding it. */
        @JavascriptInterface
        fun changeServer() {
            runOnUiThread { showServerDialog() }
        }

        @JavascriptInterface
        fun isNative(): Boolean = true
    }
}
