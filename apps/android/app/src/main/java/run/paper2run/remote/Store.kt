package run.paper2run.remote

import android.content.Context
import android.os.Build

/** What survives app restarts: which desktop we paired with, and our name. */
class Store(context: Context) {
    private val prefs = context.getSharedPreferences("p2r", Context.MODE_PRIVATE)

    var desktopId: String?
        get() = prefs.getString("desktop_id", null)
        set(v) = prefs.edit().apply { if (v == null) remove("desktop_id") else putString("desktop_id", v) }.apply()

    var deviceName: String
        get() = prefs.getString("device_name", null) ?: "${Build.MANUFACTURER} ${Build.MODEL}".trim()
        set(v) = prefs.edit().putString("device_name", v).apply()

    var port: Int
        get() = prefs.getInt("port", 0)
        set(v) = prefs.edit().putInt("port", v).apply()
}
