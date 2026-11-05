package run.paper2run.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class TunnelStatusTest {
    @Test fun parsesTheThreeStates() {
        assertEquals(TunnelStatus.Connecting, TunnelStatus.parse("""{"state":"connecting"}"""))
        assertEquals(TunnelStatus.Connected("relay"), TunnelStatus.parse("""{"state":"connected","path":"relay"}"""))
        assertEquals(TunnelStatus.Connected("direct"), TunnelStatus.parse("""{"path":"direct","state":"connected"}"""))
        assertEquals(TunnelStatus.Revoked, TunnelStatus.parse("""{"state":"revoked"}"""))
    }

    @Test fun ignoresLogNoiseAndBlankLines() {
        assertNull(TunnelStatus.parse(""))
        assertNull(TunnelStatus.parse("   "))
        assertNull(TunnelStatus.parse("2026-11-05T10:00:00Z INFO p2r_tunnel: connecting id=abc"))
        assertNull(TunnelStatus.parse("""{"state":"bogus"}"""))
        assertNull(TunnelStatus.parse("""{"state":"connected","path":"carrier-pigeon"}"""))
    }

    @Test fun labelsForTheStatusBar() {
        assertEquals("正在连接电脑…", TunnelStatus.Connecting.label)
        assertEquals("已连接 · 直连", TunnelStatus.Connected("direct").label)
        assertEquals("已连接 · 经中继", TunnelStatus.Connected("relay").label)
        assertEquals("已被电脑撤销", TunnelStatus.Revoked.label)
    }
}
