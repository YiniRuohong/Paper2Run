package run.paper2run.remote

/**
 * Retries of the page load while the local tunnel port is not accepting connections yet
 * (just after pairing, or while the tunnel restarts). Pure logic, unit-tested on the JVM.
 */
object LoadRetry {
    // android.webkit.WebViewClient error codes.
    const val ERROR_CONNECT = -6
    const val ERROR_IO = -7
    const val ERROR_TIMEOUT = -8
    const val MAX_ATTEMPTS = 10
    private val TRANSIENT = setOf(ERROR_CONNECT, ERROR_IO, ERROR_TIMEOUT)

    /** Delay before retry [attempt] (1-based), or null when the error is not transient or the attempts are used up. */
    fun delayMs(errorCode: Int, attempt: Int): Long? {
        if (errorCode !in TRANSIENT || attempt < 1 || attempt > MAX_ATTEMPTS) return null
        return minOf(500L * attempt, 3000L)
    }
}
