package ae.aus.roadsense

import org.json.JSONObject

/**
 * One timestamped observation, matching the `SensorFrame` pydantic model the
 * server accepts at /ingest.
 *
 * Units are SI and are not negotiable: speeds in metres per second, heading in
 * radians clockwise from true north, acceleration in m/s^2, rotation in rad/s.
 * The server rejects anything else, and a silent unit mismatch would poison the
 * training data rather than fail loudly.
 *
 * A null field means "no new reading", never zero. Omitting nulls from the JSON
 * is what lets the server's synchronizer age each channel independently.
 */
data class SensorFrame(
    val timestamp: Double,
    val streamId: String,
    val source: String = "android",
    val accelX: Double? = null,
    val accelY: Double? = null,
    val accelZ: Double? = null,
    val gyroX: Double? = null,
    val gyroY: Double? = null,
    val gyroZ: Double? = null,
    val gpsLatitude: Double? = null,
    val gpsLongitude: Double? = null,
    val gpsSpeed: Double? = null,
    val gpsHeading: Double? = null,
    val vehicleSpeed: Double? = null,
    val rpm: Double? = null,
    val throttlePosition: Double? = null,
    val engineLoad: Double? = null,
    val heartRate: Double? = null,
    val rrIntervalMs: Double? = null,
) {

    /** True when nothing but the timestamp is set; such a frame is not worth sending. */
    fun isEmpty(): Boolean = listOf(
        accelX, accelY, accelZ, gyroX, gyroY, gyroZ,
        gpsLatitude, gpsLongitude, gpsSpeed, gpsHeading,
        vehicleSpeed, rpm, throttlePosition, engineLoad, heartRate, rrIntervalMs,
    ).all { it == null }

    fun toJson(): JSONObject {
        val json = JSONObject()
        json.put("timestamp", timestamp)
        json.put("stream_id", streamId)
        json.put("source", source)
        putIfPresent(json, "accel_x", accelX)
        putIfPresent(json, "accel_y", accelY)
        putIfPresent(json, "accel_z", accelZ)
        putIfPresent(json, "gyro_x", gyroX)
        putIfPresent(json, "gyro_y", gyroY)
        putIfPresent(json, "gyro_z", gyroZ)
        putIfPresent(json, "gps_latitude", gpsLatitude)
        putIfPresent(json, "gps_longitude", gpsLongitude)
        putIfPresent(json, "gps_speed", gpsSpeed)
        putIfPresent(json, "gps_heading", gpsHeading)
        putIfPresent(json, "vehicle_speed", vehicleSpeed)
        putIfPresent(json, "rpm", rpm)
        putIfPresent(json, "throttle_position", throttlePosition)
        putIfPresent(json, "engine_load", engineLoad)
        putIfPresent(json, "heart_rate", heartRate)
        putIfPresent(json, "rr_interval_ms", rrIntervalMs)
        return json
    }

    private fun putIfPresent(json: JSONObject, key: String, value: Double?) {
        if (value != null && value.isFinite()) json.put(key, value)
    }
}
