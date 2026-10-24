package ae.aus.roadsense

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.ActivityCompat
import androidx.core.app.NotificationCompat
import kotlin.math.PI
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Foreground service that captures a drive.
 *
 * It owns the two Bluetooth links, the phone's inertial sensors and GPS, merges
 * them into [SensorFrame]s at a fixed cadence, and hands them to the queue.
 *
 * Running as a foreground service with a visible notification is the entire
 * point of a native app here: Android suspends background work aggressively,
 * which is exactly why the 28 September Car Scanner recording lost twenty
 * minutes in the middle.
 */
class CaptureService : Service(), SensorEventListener, LocationListener {

    private lateinit var settings: SettingsStore
    private lateinit var queue: FrameQueue
    private lateinit var uploader: Uploader
    private var obd: ObdClient? = null
    private var wearable: WhoopClient? = null

    private var scope: CoroutineScope? = null
    private var pump: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var streamId: String = ""

    // Latest values from the phone's own sensors.
    @Volatile private var accel: FloatArray? = null
    @Volatile private var gyro: FloatArray? = null
    @Volatile private var location: Location? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        settings = SettingsStore(this)
        queue = FrameQueue(this)
        uploader = Uploader(queue, settings)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopCapture()
                stopSelf()
                return START_NOT_STICKY
            }
            else -> {
                val stream = intent?.getStringExtra(EXTRA_STREAM_ID)
                if (stream.isNullOrBlank()) {
                    stopSelf()
                    return START_NOT_STICKY
                }
                startCapture(stream)
            }
        }
        // START_STICKY so Android brings the service back if it is ever killed
        // mid-drive; the queue on disk means nothing already captured is lost.
        return START_STICKY
    }

    // ------------------------------------------------------------- lifecycle

    @SuppressLint("MissingPermission")
    private fun startCapture(stream: String) {
        if (streamId == stream && pump?.isActive == true) return
        stopCapture()
        streamId = stream
        startForeground(NOTIFICATION_ID, notification("Connecting to the car"))

        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "roadsense:capture")
            .also { it.acquire(MAX_SESSION_MS) }

        registerPhoneSensors()

        val manager = getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
        val adapter: BluetoothAdapter? = manager.adapter

        val worker = CoroutineScope(Dispatchers.IO)
        scope = worker
        pump = worker.launch {
            if (adapter != null && hasBluetoothPermission()) {
                connectDevices(adapter)
            } else {
                Log.w(TAG, "No Bluetooth adapter or permission; capturing phone sensors only")
            }
            var sinceUpload = 0
            while (isActive) {
                val frame = buildFrame()
                if (!frame.isEmpty()) {
                    queue.enqueue(frame)
                    queue.trimTo(MAX_QUEUE_DEPTH)
                }
                sinceUpload++
                if (sinceUpload >= UPLOAD_EVERY_N_FRAMES) {
                    sinceUpload = 0
                    uploader.drain()
                    updateNotification()
                }
                delay(FRAME_INTERVAL_MS)
            }
        }
    }

    @SuppressLint("MissingPermission")
    private suspend fun connectDevices(adapter: BluetoothAdapter) {
        val obdClient = ObdClient(adapter)
        obd = obdClient
        val target = adapter.bondedDevices?.firstOrNull {
            it.name?.contains(settings.adapterName, ignoreCase = true) == true
        }
        if (target == null) {
            Log.w(TAG, "No bonded device matching '${settings.adapterName}'")
        } else {
            // The adapter is often slow to accept a socket right after ignition.
            repeat(OBD_CONNECT_ATTEMPTS) { attempt ->
                if (obdClient.connect(target)) return@repeat
                delay(1500L * (attempt + 1))
            }
        }

        val strap = WhoopClient(this, adapter)
        wearable = strap
        strap.bondedStrap(settings.wearableName)?.let { strap.connect(it) }
            ?: Log.w(TAG, "No bonded wearable matching '${settings.wearableName}'")
    }

    private fun stopCapture() {
        pump?.cancel()
        pump = null
        scope = null
        obd?.close()
        obd = null
        wearable?.close()
        wearable = null
        unregisterPhoneSensors()
        runCatching { if (wakeLock?.isHeld == true) wakeLock?.release() }
        wakeLock = null
        // One last attempt to clear the backlog before the process goes quiet.
        runCatching { uploader.drain() }
        streamId = ""
    }

    override fun onDestroy() {
        stopCapture()
        super.onDestroy()
    }

    // ---------------------------------------------------------------- frames

    /** Merge whatever is currently fresh into one observation. */
    private fun buildFrame(): SensorFrame {
        val reading = obd?.poll() ?: ObdClient.Reading()
        val a = accel
        val g = gyro
        val fix = location
        return SensorFrame(
            timestamp = System.currentTimeMillis() / 1000.0,
            streamId = streamId,
            accelX = a?.get(0)?.toDouble(),
            accelY = a?.get(1)?.toDouble(),
            accelZ = a?.get(2)?.toDouble(),
            gyroX = g?.get(0)?.toDouble(),
            gyroY = g?.get(1)?.toDouble(),
            gyroZ = g?.get(2)?.toDouble(),
            gpsLatitude = fix?.latitude,
            gpsLongitude = fix?.longitude,
            gpsSpeed = fix?.speed?.toDouble(),
            // Android reports bearing in degrees; the schema wants radians
            // clockwise from true north, strictly below 2*pi.
            gpsHeading = fix?.bearing?.let { (it.toDouble() * PI / 180.0) % (2 * PI) },
            vehicleSpeed = reading.vehicleSpeedMs,
            rpm = reading.rpm,
            throttlePosition = reading.throttlePercent,
            engineLoad = reading.engineLoadPercent,
            heartRate = wearable?.heartRate,
            rrIntervalMs = wearable?.rrIntervalMs,
        )
    }

    // --------------------------------------------------------- phone sensors

    @SuppressLint("MissingPermission")
    private fun registerPhoneSensors() {
        val sensors = getSystemService(Context.SENSOR_SERVICE) as SensorManager
        sensors.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)?.let {
            sensors.registerListener(this, it, SensorManager.SENSOR_DELAY_GAME)
        }
        sensors.getDefaultSensor(Sensor.TYPE_GYROSCOPE)?.let {
            sensors.registerListener(this, it, SensorManager.SENSOR_DELAY_GAME)
        }
        if (hasLocationPermission()) {
            val locations = getSystemService(Context.LOCATION_SERVICE) as LocationManager
            runCatching {
                locations.requestLocationUpdates(
                    LocationManager.GPS_PROVIDER, 1000L, 0f, this
                )
            }.onFailure { Log.w(TAG, "GPS unavailable: ${it.message}") }
        }
    }

    private fun unregisterPhoneSensors() {
        runCatching {
            (getSystemService(Context.SENSOR_SERVICE) as SensorManager).unregisterListener(this)
        }
        runCatching {
            (getSystemService(Context.LOCATION_SERVICE) as LocationManager)
                .removeUpdates(this)
        }
    }

    override fun onSensorChanged(event: SensorEvent) {
        when (event.sensor.type) {
            Sensor.TYPE_ACCELEROMETER -> accel = event.values.clone()
            Sensor.TYPE_GYROSCOPE -> gyro = event.values.clone()
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit

    override fun onLocationChanged(fix: Location) {
        location = fix
    }

    // --------------------------------------------------------- notification

    private fun notification(status: String): Notification {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID, "Drive capture", NotificationManager.IMPORTANCE_LOW
                ).apply { description = "Shown while a driving session is recording" }
            )
        }
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Recording your drive")
            .setContentText(status)
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .setOngoing(true)
            .setContentIntent(open)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    /** Keep the notification honest about what is and is not connected. */
    private fun updateNotification() {
        val parts = mutableListOf<String>()
        parts.add(if (obd?.connected == true) "Car connected" else "Car not connected")
        parts.add(if (wearable?.connected == true) "wearable connected" else "no wearable")
        val backlog = queue.depth()
        if (backlog > 0) parts.add("$backlog frames waiting to upload")
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.notify(NOTIFICATION_ID, notification(parts.joinToString(" · ")))
    }

    // ---------------------------------------------------------- permissions

    private fun hasBluetoothPermission(): Boolean =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) ==
                PackageManager.PERMISSION_GRANTED
        } else {
            true
        }

    private fun hasLocationPermission(): Boolean =
        ActivityCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    companion object {
        const val ACTION_STOP = "ae.aus.roadsense.STOP"
        const val EXTRA_STREAM_ID = "stream_id"

        private const val TAG = "CaptureService"
        private const val CHANNEL_ID = "roadsense-capture"
        private const val NOTIFICATION_ID = 42
        /** 5 Hz: comfortably above the 1 Hz the specification requires. */
        private const val FRAME_INTERVAL_MS = 200L
        private const val UPLOAD_EVERY_N_FRAMES = 10
        private const val OBD_CONNECT_ATTEMPTS = 4
        private const val MAX_QUEUE_DEPTH = 200_000
        private const val MAX_SESSION_MS = 6 * 60 * 60 * 1000L

        fun start(context: Context, streamId: String) {
            val intent = Intent(context, CaptureService::class.java)
                .putExtra(EXTRA_STREAM_ID, streamId)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, CaptureService::class.java).setAction(ACTION_STOP)
            )
        }
    }
}
