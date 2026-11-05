package run.paper2run.remote

/** Maps `p2r-tunnel pair` exit codes (0 ok, 3 wrong code, 4 unknown/expired) to UI outcomes. */
sealed class PairResult {
    data object Paired : PairResult()
    data class Failed(val message: String) : PairResult()

    companion object {
        fun from(exitCode: Int, stderr: String): PairResult = when (exitCode) {
            0 -> Paired
            3 -> Failed("配对码不对，这个码已作废，请在电脑上重新生成")
            4 -> Failed("配对码不存在或已过期")
            else -> {
                val last = stderr.lines().map { it.trim() }.lastOrNull { it.isNotEmpty() }
                    ?.removePrefix("pairing failed: ")
                if (last == null) Failed("配对失败（退出码 $exitCode）") else Failed("配对失败：$last")
            }
        }
    }
}
