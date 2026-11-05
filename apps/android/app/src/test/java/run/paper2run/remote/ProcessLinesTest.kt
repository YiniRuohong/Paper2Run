package run.paper2run.remote

import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.InputStream
import java.io.InterruptedIOException

class ProcessLinesTest {
    @Test fun deliversEveryLine() {
        val seen = mutableListOf<String>()
        ProcessLines.drain(ByteArrayInputStream("a\nb\n\nc".toByteArray())) { seen += it }
        assertEquals(listOf("a", "b", "", "c"), seen)
    }

    /** Destroying the tunnel process closes its pipes under the reader thread; that must not crash the app. */
    @Test fun streamClosedUnderTheReaderEndsQuietly() {
        val first = "x\n".toByteArray()
        val closing = object : InputStream() {
            var pos = 0
            override fun read(): Int {
                if (pos < first.size) return first[pos++].toInt()
                throw InterruptedIOException("read interrupted by close() on another thread")
            }
        }
        val seen = mutableListOf<String>()
        ProcessLines.drain(closing) { seen += it }
        assertEquals(listOf("x"), seen)
    }
}
