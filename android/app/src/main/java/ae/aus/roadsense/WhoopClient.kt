package ae.aus.roadsense

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.util.Log
import java.util.UUID

/**
 * Receives heart rate over the standard BLE Heart Rate Service.
 *
 * WHOOP must have Heart Rate Broadcast switched on in its own app, and that
 * setting is known to reset after a strap firmware update, so [connected] is
 * surfaced to the driver rather than assumed. The service is the Bluetooth SIG
 * standard, so a Polar or Garmin strap works here unchanged.
 *
 * WHOOP broadcasts heart rate and RR intervals only. There is no SpO2 and no
 * temperature on this channel, whatever the wearable measures internally.
 */
class WhoopClient(private val context: Context, private val adapter: BluetoothAdapter) {

    private val heartRateService: UUID = UUID.fromString("0000180d-0000-1000-8000-00805f9b34fb")
    private val heartRateMeasurement: UUID = UUID.fromString("00002a37-0000-1000-8000-00805f9b34fb")
    private val clientConfig: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

    private var gatt: BluetoothGatt? = null

    @Volatile
    var connected: Boolean = false
        private set

    /** Most recent heart rate in bpm, or null if nothing has arrived yet. */
    @Volatile
    var heartRate: Double? = null
        private set

    /** Most recent RR (beat-to-beat) interval in milliseconds. HRV derives from these. */
    @Volatile
    var rrIntervalMs: Double? = null
        private set

    private val callback = object : BluetoothGattCallback() {
        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                g.discoverServices()
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                connected = false
                heartRate = null
                rrIntervalMs = null
            }
        }

        @SuppressLint("MissingPermission")
        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            val characteristic = g.getService(heartRateService)
                ?.getCharacteristic(heartRateMeasurement)
            if (characteristic == null) {
                Log.w(TAG, "Strap exposes no heart rate service; is broadcast enabled?")
                return
            }
            g.setCharacteristicNotification(characteristic, true)
            // Subscribing also needs the Client Characteristic Configuration
            // descriptor written, or no notifications ever arrive.
            characteristic.getDescriptor(clientConfig)?.let { descriptor ->
                @Suppress("DEPRECATION")
                descriptor.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                @Suppress("DEPRECATION")
                g.writeDescriptor(descriptor)
            }
            connected = true
        }

        @Deprecated("Kept for API 32 and below; the API 33 overload delegates here.")
        @Suppress("DEPRECATION")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            parse(c.value)
        }

        override fun onCharacteristicChanged(
            g: BluetoothGatt,
            c: BluetoothGattCharacteristic,
            value: ByteArray,
        ) {
            parse(value)
        }
    }

    /**
     * Decode a Heart Rate Measurement packet.
     *
     * Byte 0 is a flags field: bit 0 says whether the rate is 8-bit or 16-bit,
     * bit 3 says whether energy expended is present, bit 4 says whether RR
     * intervals follow. RR values are in 1/1024 second units.
     */
    private fun parse(value: ByteArray?) {
        if (value == null || value.isEmpty()) return
        val flags = value[0].toInt() and 0xFF
        val wide = flags and 0x01 == 1
        var offset = 1
        heartRate = if (wide) {
            if (value.size < 3) return
            val rate = ((value[2].toInt() and 0xFF) shl 8) or (value[1].toInt() and 0xFF)
            offset = 3
            rate.toDouble()
        } else {
            if (value.size < 2) return
            offset = 2
            (value[1].toInt() and 0xFF).toDouble()
        }
        if (flags and 0x08 != 0) offset += 2      // skip energy expended
        if (flags and 0x10 != 0 && value.size >= offset + 2) {
            val raw = ((value[offset + 1].toInt() and 0xFF) shl 8) or
                (value[offset].toInt() and 0xFF)
            rrIntervalMs = raw * 1000.0 / 1024.0
        }
    }

    @SuppressLint("MissingPermission")
    fun connect(device: BluetoothDevice) {
        close()
        gatt = device.connectGatt(context, true, callback, BluetoothDevice.TRANSPORT_LE)
    }

    /** Find a bonded strap by name, so the driver never types an address. */
    @SuppressLint("MissingPermission")
    fun bondedStrap(nameFragment: String = "whoop"): BluetoothDevice? =
        adapter.bondedDevices?.firstOrNull {
            it.name?.contains(nameFragment, ignoreCase = true) == true
        }

    @SuppressLint("MissingPermission")
    fun close() {
        runCatching { gatt?.close() }
        gatt = null
        connected = false
        heartRate = null
        rrIntervalMs = null
    }

    private companion object {
        const val TAG = "WhoopClient"
    }
}
