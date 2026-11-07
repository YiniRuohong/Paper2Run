package run.paper2run.remote

import android.app.Activity
import android.content.ContentValues
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.print.PrintAttributes
import android.print.PrintManager
import android.provider.MediaStore
import android.util.Base64
import android.util.Log
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.widget.Toast
import java.io.File
import java.io.OutputStream
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

/**
 * `window.P2RAndroid` for assets/bridge.js: saves blob downloads into
 * Downloads/Paper2Run and opens the system print dialog for window.print().
 * Only pages from the tunnel's loopback origin are ever loaded (see MainActivity).
 */
class WebBridge(private val activity: Activity, private val web: WebView) {
    private class Pending(val name: String, val uri: Uri?, val file: File?, val out: OutputStream)

    private val pending = ConcurrentHashMap<String, Pending>()

    @JavascriptInterface
    fun begin(rawName: String, mime: String): String {
        val name = DownloadName.safe(rawName)
        val id = UUID.randomUUID().toString()
        pending[id] = if (Build.VERSION.SDK_INT >= 29) {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.MIME_TYPE, mime.ifBlank { "application/octet-stream" })
                put(MediaStore.Downloads.RELATIVE_PATH, "${Environment.DIRECTORY_DOWNLOADS}/Paper2Run")
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val uri = activity.contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: error("cannot create $name in Downloads")
            Pending(name, uri, null, activity.contentResolver.openOutputStream(uri) ?: error("cannot open $name"))
        } else {
            val dir = activity.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: activity.filesDir
            val file = File(dir, name)
            Pending(name, null, file, file.outputStream())
        }
        toast("正在保存 $name")
        return id
    }

    @JavascriptInterface
    fun chunk(id: String, base64: String) {
        val p = pending[id] ?: return
        p.out.write(Base64.decode(base64, Base64.DEFAULT))
    }

    @JavascriptInterface
    fun end(id: String) {
        val p = pending.remove(id) ?: return
        p.out.close()
        if (p.uri != null) {
            activity.contentResolver.update(p.uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
            toast("已保存到 下载/Paper2Run/${p.name}")
        } else {
            toast("已保存到 ${p.file?.path}")
        }
    }

    @JavascriptInterface
    fun fail(id: String, message: String) {
        Log.w("p2r", "download failed: $message")
        pending.remove(id)?.let { p ->
            runCatching { p.out.close() }
            p.uri?.let { runCatching { activity.contentResolver.delete(it, null, null) } }
            p.file?.delete()
        }
        toast("下载失败：$message")
    }

    @JavascriptInterface
    fun print(title: String) {
        activity.runOnUiThread {
            val job = DownloadName.safe(title.ifBlank { "Paper2Run" })
            val inner = web.createPrintDocumentAdapter(job)
            val manager = activity.getSystemService(PrintManager::class.java)
            // The WebView's own adapter fires the page's beforeprint/afterprint events.
            manager.print(job, inner, PrintAttributes.Builder().setMediaSize(PrintAttributes.MediaSize.ISO_A4).build())
        }
    }

    private fun toast(text: String) = activity.runOnUiThread { Toast.makeText(activity, text, Toast.LENGTH_SHORT).show() }
}
