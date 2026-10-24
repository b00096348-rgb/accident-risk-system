package ae.aus.roadsense

import android.content.Context

/** App settings. The server address is configurable so hosting can change later. */
class SettingsStore(context: Context) {

    private val prefs = context.getSharedPreferences("roadsense", Context.MODE_PRIVATE)

    /** Base URL of the FastAPI server, with no trailing slash. */
    var serverUrl: String
        get() = prefs.getString(KEY_SERVER, DEFAULT_SERVER).orEmpty()
        set(value) = prefs.edit().putString(KEY_SERVER, value.trim().trimEnd('/')).apply()

    /** Bluetooth name fragment identifying the OBD adapter among bonded devices. */
    var adapterName: String
        get() = prefs.getString(KEY_ADAPTER, "OBD").orEmpty()
        set(value) = prefs.edit().putString(KEY_ADAPTER, value.trim()).apply()

    /** Bluetooth name fragment identifying the wearable. */
    var wearableName: String
        get() = prefs.getString(KEY_WEARABLE, "WHOOP").orEmpty()
        set(value) = prefs.edit().putString(KEY_WEARABLE, value.trim()).apply()

    private companion object {
        const val KEY_SERVER = "server_url"
        const val KEY_ADAPTER = "adapter_name"
        const val KEY_WEARABLE = "wearable_name"
        // The emulator's alias for the host machine, so a fresh install can be
        // tested against a laptop without typing anything.
        const val DEFAULT_SERVER = "http://10.0.2.2:8000"
    }
}
