package run.paper2run.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class LoadRetryTest {
    @Test fun retriesWhileTheTunnelPortIsNotAcceptingYet() {
        assertEquals(500L, LoadRetry.delayMs(LoadRetry.ERROR_CONNECT, 1))
        assertEquals(1000L, LoadRetry.delayMs(LoadRetry.ERROR_CONNECT, 2))
        assertEquals(500L, LoadRetry.delayMs(LoadRetry.ERROR_TIMEOUT, 1))
        assertEquals(500L, LoadRetry.delayMs(LoadRetry.ERROR_IO, 1))
    }

    @Test fun backoffIsCapped() {
        assertEquals(3000L, LoadRetry.delayMs(LoadRetry.ERROR_CONNECT, LoadRetry.MAX_ATTEMPTS))
    }

    @Test fun givesUpAfterTheLimitAndOnOtherErrors() {
        assertNull(LoadRetry.delayMs(LoadRetry.ERROR_CONNECT, LoadRetry.MAX_ATTEMPTS + 1))
        assertNull(LoadRetry.delayMs(-12 /* ERROR_BAD_URL */, 1))
        assertNull(LoadRetry.delayMs(LoadRetry.ERROR_CONNECT, 0))
    }
}
