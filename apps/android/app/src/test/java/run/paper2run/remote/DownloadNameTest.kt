package run.paper2run.remote

import org.junit.Assert.assertEquals
import org.junit.Test

class DownloadNameTest {
    @Test fun keepsOrdinaryNames() {
        assertEquals("paper2run-abc.zip", DownloadName.safe("paper2run-abc.zip"))
        assertEquals("结果 图1.png", DownloadName.safe("结果 图1.png"))
    }

    @Test fun dropsDirectoriesAndReservedCharacters() {
        assertEquals("passwd", DownloadName.safe("../../etc/passwd"))
        assertEquals("x.txt", DownloadName.safe("C:\\tmp\\x.txt"))
        assertEquals("a_b_c.txt", DownloadName.safe("a:b?c.txt"))
    }

    @Test fun neverEmpty() {
        assertEquals("download", DownloadName.safe(""))
        assertEquals("download", DownloadName.safe(".."))
        assertEquals("download", DownloadName.safe("dir/"))
    }
}
