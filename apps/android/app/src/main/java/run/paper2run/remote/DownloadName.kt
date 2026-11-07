package run.paper2run.remote

/** File names handed over by the page become plain names in Downloads. */
object DownloadName {
    private val unsafe = Regex("""[\\/:*?"<>|\u0000-\u001f]""")

    fun safe(raw: String): String {
        val base = raw.substringAfterLast('/').substringAfterLast('\\')
        val cleaned = unsafe.replace(base, "_").trim().trim('.').take(120)
        return cleaned.ifEmpty { "download" }
    }
}
