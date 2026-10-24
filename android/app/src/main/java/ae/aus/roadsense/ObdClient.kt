package ae.aus.roadsense

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothSocket
import android.util.Log
import java.io.InputStream
import java.io.OutputStream
import java.util.UUID

/**
 * Talks to an ELM327 adapter over Bluetooth Classic (SPP).
 *
 * Deliberately polls only the parameters the risk model consumes. Car Scanner's
 * problem was breadth: the adapter answers one request at a time over a single
 * serial link, so every extra PID steals sample rate from the ones that matter.
 */
class ObdClient(private val adapter: BluetoothAdapter) {

    /** Standard Serial Port Profile UUID. Every ELM327 clone exposes this. */
    private val sppUuid: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")

    private var socket: BluetoothSocket? = null
    private var input: InputStream? = null
    private var output: OutputStream? = null

    /** The last value read for each PID, in the units the wire schema expects. */
    data class Reading(
        val vehicleSpeedMs: Double? = null,
        val rpm: Double? = null,
        val throttlePercent: Double? = null,
        val engineLoadPercent: Double? = null,
        val coolantCelsius: Double? = null,
        val shortTermFuelTrim: Double? = null,
        val longTermFuelTrim: Double? = null,
    )

    /** Mode 01 PIDs, in polling order. Speed and RPM come first and most often. */
    private enum class Pid(val code: String, val bytes: Int) {
        SPEED("0D", 1),
        RPM("0C", 2),
        THROTTLE("11", 1),
        ENGINE_LOAD("04", 1),
        COOLANT("05", 1),
        SHORT_FUEL_TRIM("06", 1),
        LONG_FUEL_TRIM("07", 1),
    }

    /** Fast channels are polled on every pass, slow ones every eighth. */
    private val fast = listOf(Pid.SPEED, Pid.RPM, Pid.THROTTLE, Pid.ENGINE_LOAD)
    private val slow = listOf(Pid.COOLANT, Pid.SHORT_FUEL_TRIM, Pid.LONG_FUEL_TRIM)
    private var pass = 0

    @Volatile
    var connected: Boolean = false
        private set

    /**
     * Pair with [device] and bring the adapter into a known state.
     * Returns false if the socket or the handshake fails.
     */
    @SuppressLint("MissingPermission")
    fun connect(device: BluetoothDevice): Boolean {
        close()
        return try {
            adapter.cancelDiscovery()
            val created = device.createRfcommSocketToServiceRecord(sppUuid)
            created.connect()
            socket = created
            input = created.inputStream
            output = created.outputStream
            // ATZ resets; ATE0 stops the adapter echoing our own commands back;
            // ATL0/ATS0 strip linefeeds and spaces; ATH0 hides headers;
            // ATSP0 lets the adapter negotiate the protocol itself.
            listOf("ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP0").forEach {
                send(it)
                Thread.sleep(120)
            }
            // 0100 asks which PIDs the ECU supports; a sane reply proves the link.
            val probe = send("0100")
            connected = probe.contains("41")
            if (!connected) Log.w(TAG, "Adapter answered but the ECU did not: $probe")
            connected
        } catch (e: Exception) {
            Log.w(TAG, "OBD connect failed", e)
            close()
            false
        }
    }

    /** Poll one round of PIDs. Null fields mean no fresh value this pass. */
    fun poll(): Reading {
        if (!connected) return Reading()
        val due = if (pass % 8 == 0) fast + slow else fast
        pass++
        var reading = Reading()
        for (pid in due) {
            val raw = decode(pid, send("01${pid.code}")) ?: continue
            reading = when (pid) {
                // The wire schema is SI: OBD reports km/h, we publish m/s.
                Pid.SPEED -> reading.copy(vehicleSpeedMs = raw / 3.6)
                Pid.RPM -> reading.copy(rpm = raw)
                Pid.THROTTLE -> reading.copy(throttlePercent = raw)
                Pid.ENGINE_LOAD -> reading.copy(engineLoadPercent = raw)
                Pid.COOLANT -> reading.copy(coolantCelsius = raw)
                Pid.SHORT_FUEL_TRIM -> reading.copy(shortTermFuelTrim = raw)
                Pid.LONG_FUEL_TRIM -> reading.copy(longTermFuelTrim = raw)
            }
        }
        return reading
    }

    /**
     * Convert an ELM327 reply into an engineering value.
     *
     * A reply to `01 0D` looks like `410D2B`: `41` means "answer to mode 01",
     * `0D` echoes the PID, and the rest are the data bytes. The scaling for each
     * PID is fixed by SAE J1979.
     */
    private fun decode(pid: Pid, reply: String): Double? {
        val cleaned = reply.replace(Regex("[^0-9A-Fa-f]"), "").uppercase()
        val marker = "41" + pid.code
        val start = cleaned.indexOf(marker)
        if (start < 0) return null
        val payload = cleaned.substring(start + marker.length)
        if (payload.length < pid.bytes * 2) return null
        val a = payload.substring(0, 2).toIntOrNull(16) ?: return null
        val b = if (pid.bytes > 1) payload.substring(2, 4).toIntOrNull(16) ?: return null else 0
        return when (pid) {
            Pid.SPEED -> a.toDouble()                                  // km/h
            Pid.RPM -> (256.0 * a + b) / 4.0                           // rev/min
            Pid.THROTTLE, Pid.ENGINE_LOAD -> a * 100.0 / 255.0         // percent
            Pid.COOLANT -> a - 40.0                                    // degrees C
            Pid.SHORT_FUEL_TRIM, Pid.LONG_FUEL_TRIM -> (a - 128) * 100.0 / 128.0
        }
    }

    /** Write a command and read until the adapter's '>' prompt. */
    private fun send(command: String): String {
        val out = output ?: return ""
        val source = input ?: return ""
        out.write("$command\r".toByteArray())
        out.flush()
        val builder = StringBuilder()
        val buffer = ByteArray(128)
        val deadline = System.currentTimeMillis() + READ_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline) {
            if (source.available() > 0) {
                val read = source.read(buffer)
                if (read > 0) {
                    builder.append(String(buffer, 0, read))
                    if (builder.contains('>')) break
                }
            } else {
                Thread.sleep(8)
            }
        }
        return builder.toString().replace(">", "").trim()
    }

    fun close() {
        connected = false
        runCatching { socket?.close() }
        socket = null
        input = null
        output = null
    }

    private companion object {
        const val TAG = "ObdClient"
        const val READ_TIMEOUT_MS = 400L
    }
}
