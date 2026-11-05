package run.paper2run.remote

/** State lines printed by `p2r-tunnel connect --status-json`. */
sealed class TunnelStatus {
    data object Connecting : TunnelStatus()
    data class Connected(val path: String) : TunnelStatus()
    data object Revoked : TunnelStatus()

    val label: String
        get() = when (this) {
            Connecting -> "正在连接电脑…"
            is Connected -> if (path == "direct") "已连接 · 直连" else "已连接 · 经中继"
            Revoked -> "已被电脑撤销"
        }

    companion object {
        private fun field(line: String, name: String): String? =
            Regex("\"$name\"\\s*:\\s*\"([a-z]+)\"").find(line)?.groupValues?.get(1)

        /** Returns null for anything that is not a known status line (log noise, blanks). */
        fun parse(line: String): TunnelStatus? {
            val t = line.trim()
            if (!t.startsWith("{") || !t.endsWith("}")) return null
            return when (field(t, "state")) {
                "connecting" -> Connecting
                "revoked" -> Revoked
                "connected" -> field(t, "path")?.takeIf { it == "direct" || it == "relay" }?.let { Connected(it) }
                else -> null
            }
        }
    }
}
