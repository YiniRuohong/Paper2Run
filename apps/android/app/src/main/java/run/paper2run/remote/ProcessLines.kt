package run.paper2run.remote

import java.io.IOException
import java.io.InputStream

/** Reads a child process's output line by line on a background thread. */
object ProcessLines {
    /**
     * Calls [onLine] for each line until the stream ends. When the process is
     * destroyed its pipes close under this reader and the read throws; that is
     * a normal end here, never a crash.
     */
    fun drain(input: InputStream, onLine: (String) -> Unit) {
        try {
            input.bufferedReader().forEachLine(onLine)
        } catch (_: IOException) {
            // stream closed: process gone
        }
    }
}
