package ae.aus.roadsense

import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.net.HttpURLConnection
import java.net.URL

/**
 * Drains the frame queue to the server.
 *
 * Single frames go to /ingest while the link is healthy. After a dropout the
 * backlog goes through /ingest/batch, which reports acceptance per item, so one
 * bad frame cannot block the thousands behind it.
 */
class Uploader(private val queue: FrameQueue, private val settings: SettingsStore) {

    @Volatile
    var online: Boolean = false
        private set

    /**
     * Push as much of the queue as the server will take.
     * Returns the number of frames accepted.
     */
    fun drain(): Int {
        val base = settings.serverUrl.trimEnd('/')
        if (base.isEmpty()) return 0
        var accepted = 0
        while (true) {
            val batch = queue.peek(BATCH_SIZE)
            if (batch.isEmpty()) break
            val sent = if (batch.size == 1) {
                if (postSingle(base, batch[0].second)) listOf(batch[0].first) else null
            } else {
                postBatch(base, batch)
            }
            if (sent == null) {
                online = false
                return accepted
            }
            queue.discard(sent)
            accepted += sent.size
            online = true
            if (batch.size < BATCH_SIZE) break
        }
        return accepted
    }

    private fun postSingle(base: String, payload: JSONObject): Boolean {
        val (code, _) = post("$base/ingest", payload.toString()) ?: return false
        // 422 means the server will never accept this frame; drop it rather
        // than retry it forever behind everything else.
        return code in 200..299 || code == 422
    }

    /** Returns the row ids the server accepted, or null if the request failed. */
    private fun postBatch(base: String, batch: List<Pair<Long, JSONObject>>): List<Long>? {
        val frames = JSONArray()
        batch.forEach { frames.put(it.second) }
        val body = JSONObject().put("frames", frames).toString()
        val (code, text) = post("$base/ingest/batch", body) ?: return null
        if (code !in 200..299) {
            // A 409 means the session is stopped server-side: these frames are
            // never going to land, so clear them instead of looping.
            return if (code == 409) batch.map { it.first } else null
        }
        return runCatching {
            val results = JSONObject(text).getJSONArray("results")
            val keep = mutableListOf<Long>()
            for (i in 0 until results.length()) {
                val item = results.getJSONObject(i)
                val index = item.getInt("index")
                // Accepted frames are done; rejected ones are malformed and
                // will never be accepted, so both leave the queue.
                if (index in batch.indices) keep.add(batch[index].first)
            }
            keep
        }.getOrElse { batch.map { it.first } }
    }

    private fun post(url: String, body: String): Pair<Int, String>? = try {
        val connection = (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = CONNECT_TIMEOUT_MS
            readTimeout = READ_TIMEOUT_MS
            doOutput = true
            setRequestProperty("Content-Type", "application/json")
        }
        connection.outputStream.use { it.write(body.toByteArray()) }
        val code = connection.responseCode
        val stream = if (code in 200..299) connection.inputStream else connection.errorStream
        val text = stream?.bufferedReader()?.use(BufferedReader::readText).orEmpty()
        connection.disconnect()
        code to text
    } catch (e: Exception) {
        Log.d(TAG, "upload failed: ${e.message}")
        null
    }

    private companion object {
        const val TAG = "Uploader"
        const val BATCH_SIZE = 200
        const val CONNECT_TIMEOUT_MS = 4000
        const val READ_TIMEOUT_MS = 8000
    }
}
