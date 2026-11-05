package run.paper2run.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PairCodeTest {
    @Test fun acceptsDashedPlainAndSpaced() {
        assertEquals("123-45678", PairCode.normalize("123-45678"))
        assertEquals("123-45678", PairCode.normalize("12345678"))
        assertEquals("123-45678", PairCode.normalize(" 123 45678 "))
        assertEquals("007-00042", PairCode.normalize("00700042"))
    }

    @Test fun rejectsWrongLengthOrLetters() {
        for (bad in listOf("", "1234567", "123456789", "12a-45678", "１２３４５６７８")) {
            assertNull(bad, PairCode.normalize(bad))
        }
    }
}
