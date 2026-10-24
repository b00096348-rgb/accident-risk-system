package ae.aus.roadsense

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import org.json.JSONObject

/**
 * Durable on-disk buffer for frames that have not reached the server yet.
 *
 * This is what makes a drive through a signal black spot survivable (FR13.4).
 * Frames go in here first and leave only once the server has accepted them, so
 * the app can be killed, the phone can lose signal for twenty minutes, and the
 * session still ends up complete.
 */
class FrameQueue(context: Context) :
    SQLiteOpenHelper(context, "roadsense-queue.db", null, 1) {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE pending (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id  TEXT NOT NULL,
                payload    TEXT NOT NULL,
                queued_at  REAL NOT NULL
            )
            """.trimIndent()
        )
        db.execSQL("CREATE INDEX idx_pending_stream ON pending(stream_id, id)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        db.execSQL("DROP TABLE IF EXISTS pending")
        onCreate(db)
    }

    fun enqueue(frame: SensorFrame) {
        val values = ContentValues().apply {
            put("stream_id", frame.streamId)
            put("payload", frame.toJson().toString())
            put("queued_at", System.currentTimeMillis() / 1000.0)
        }
        writableDatabase.insert("pending", null, values)
    }

    /** Oldest-first batch of queued frames, as (rowId, payload) pairs. */
    fun peek(limit: Int): List<Pair<Long, JSONObject>> {
        val rows = mutableListOf<Pair<Long, JSONObject>>()
        readableDatabase.rawQuery(
            "SELECT id, payload FROM pending ORDER BY id LIMIT ?", arrayOf(limit.toString())
        ).use { cursor ->
            while (cursor.moveToNext()) {
                val id = cursor.getLong(0)
                runCatching { JSONObject(cursor.getString(1)) }
                    .onSuccess { rows.add(id to it) }
                    .onFailure { discard(listOf(id)) }   // unparseable, never retry
            }
        }
        return rows
    }

    /** Remove frames the server has accepted. */
    fun discard(ids: List<Long>) {
        if (ids.isEmpty()) return
        val placeholders = ids.joinToString(",") { "?" }
        writableDatabase.execSQL(
            "DELETE FROM pending WHERE id IN ($placeholders)",
            ids.map { it.toString() }.toTypedArray()
        )
    }

    fun depth(): Int =
        readableDatabase.rawQuery("SELECT COUNT(*) FROM pending", null).use {
            if (it.moveToFirst()) it.getInt(0) else 0
        }

    /**
     * Drop the oldest frames once the backlog passes [limit].
     *
     * A phone left queueing for days would otherwise fill its own storage. The
     * newest data is the data worth keeping.
     */
    fun trimTo(limit: Int) {
        writableDatabase.execSQL(
            "DELETE FROM pending WHERE id NOT IN " +
                "(SELECT id FROM pending ORDER BY id DESC LIMIT ?)",
            arrayOf(limit.toString())
        )
    }
}
