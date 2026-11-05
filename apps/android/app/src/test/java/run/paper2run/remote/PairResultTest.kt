package run.paper2run.remote

import org.junit.Assert.assertEquals
import org.junit.Test

class PairResultTest {
    @Test fun mapsExitCodes() {
        assertEquals(PairResult.Paired, PairResult.from(0, ""))
        assertEquals(PairResult.Failed("配对码不对，这个码已作废，请在电脑上重新生成"), PairResult.from(3, "pairing failed: wrong"))
        assertEquals(PairResult.Failed("配对码不存在或已过期"), PairResult.from(4, "pairing failed: gone"))
    }

    @Test fun otherFailuresShowTheLastStderrLine() {
        assertEquals(PairResult.Failed("配对失败：network unreachable"), PairResult.from(1, "INFO starting\npairing failed: network unreachable\n"))
        assertEquals(PairResult.Failed("配对失败（退出码 9）"), PairResult.from(9, "   \n"))
    }
}
