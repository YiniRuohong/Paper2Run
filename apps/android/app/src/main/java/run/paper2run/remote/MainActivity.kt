package run.paper2run.remote

import android.Manifest
import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.view.ViewGroup
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : ComponentActivity() {
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private val pickFiles = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        fileCallback?.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data))
        fileCallback = null
    }
    private val askNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = Store(this)
        setContent {
            MaterialTheme {
                Surface(Modifier.fillMaxSize().safeDrawingPadding()) {
                    var paired by remember { mutableStateOf(store.desktopId != null) }
                    if (paired) {
                        MainScreen(onUnpaired = { paired = false })
                    } else {
                        PairScreen(store, onPaired = { paired = true })
                    }
                }
            }
        }
    }

    @Composable
    private fun PairScreen(store: Store, onPaired: () -> Unit) {
        var code by remember { mutableStateOf("") }
        var name by remember { mutableStateOf(store.deviceName) }
        var busy by remember { mutableStateOf(false) }
        var error by remember { mutableStateOf<String?>(null) }
        val scope = rememberCoroutineScope()
        val normalized = PairCode.normalize(code)
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Column(Modifier.widthIn(max = 460.dp).padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
                Text("连接到电脑上的 Paper2Run", fontSize = 24.sp)
                Text("在电脑上打开 Paper2Run → 设置 → 平板远程 → 生成配对码，然后在这里输入。", color = Color.Gray)
                OutlinedTextField(
                    value = code, onValueChange = { code = it.take(12) }, singleLine = true,
                    label = { Text("配对码（如 123-45678）") },
                    textStyle = MaterialTheme.typography.headlineSmall.copy(fontFamily = FontFamily.Monospace),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.fillMaxWidth(),
                )
                OutlinedTextField(
                    value = name, onValueChange = { name = it.take(64) }, singleLine = true,
                    label = { Text("这台平板的名称（显示在电脑上）") }, modifier = Modifier.fillMaxWidth(),
                )
                error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                Button(
                    enabled = !busy && normalized != null && name.isNotBlank(),
                    modifier = Modifier.fillMaxWidth(),
                    onClick = {
                        busy = true; error = null
                        val n = name.trim()
                        store.deviceName = n
                        scope.launch {
                            val run = withContext(Dispatchers.IO) {
                                try { Tunnel.pair(this@MainActivity, normalized!!, n) } catch (e: Exception) { Tunnel.Run(1, "pairing failed: ${e.message}") }
                            }
                            busy = false
                            when (val r = PairResult.from(run.exitCode, run.stderr)) {
                                PairResult.Paired -> onPaired()
                                is PairResult.Failed -> error = r.message
                            }
                        }
                    },
                ) { Text(if (busy) "正在配对…" else "配对") }
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    @Composable
    private fun MainScreen(onUnpaired: () -> Unit) {
        LaunchedEffect(Unit) {
            TunnelService.start(this@MainActivity)
            if (Build.VERSION.SDK_INT >= 33 &&
                checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
        val status by TunnelService.status.collectAsState()
        val port by TunnelService.port.collectAsState()
        var web by remember { mutableStateOf<WebView?>(null) }
        var loaded by remember { mutableStateOf(false) }
        var confirmUnpair by remember { mutableStateOf(false) }
        var canGoBack by remember { mutableStateOf(false) }

        LaunchedEffect(status, port, web) {
            val w = web ?: return@LaunchedEffect
            if (status is TunnelStatus.Connected && port != 0 && !loaded) {
                w.loadUrl("http://127.0.0.1:$port/lab")
                loaded = true
            }
        }
        BackHandler(enabled = canGoBack) { web?.goBack() }

        Column(Modifier.fillMaxSize()) {
            Row(
                Modifier.fillMaxWidth().background(barColor(status)).padding(horizontal = 16.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(status.label, color = Color.White, fontSize = 14.sp)
                Spacer(Modifier.weight(1f))
                TextButton(onClick = { confirmUnpair = true }) { Text("重新配对", color = Color.White) }
            }
            Box(Modifier.weight(1f)) {
                AndroidView(modifier = Modifier.fillMaxSize(), factory = { ctx ->
                    WebView(ctx).apply {
                        // Without explicit params the WebView measures to its content, and
                        // the page (height: 100dvh) then sees a 0-px viewport and draws nothing.
                        layoutParams = ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
                        settings.javaScriptEnabled = true
                        settings.domStorageEnabled = true
                        webViewClient = object : WebViewClient() {
                            override fun doUpdateVisitedHistory(view: WebView, url: String?, isReload: Boolean) {
                                canGoBack = view.canGoBack()
                            }
                        }
                        webChromeClient = object : WebChromeClient() {
                            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                                fileCallback?.onReceiveValue(null)
                                fileCallback = callback
                                return try { pickFiles.launch(params.createIntent()); true } catch (_: Exception) { fileCallback = null; false }
                            }
                        }
                        setDownloadListener { url, _, disposition, mime, _ -> download(url, disposition, mime) }
                        web = this
                    }
                })
                if (!loaded) {
                    Text("正在连接电脑…", Modifier.align(Alignment.Center), color = Color.Gray)
                }
            }
        }

        if (status is TunnelStatus.Revoked) {
            AlertDialog(
                onDismissRequest = {},
                title = { Text("已被电脑撤销") },
                text = { Text("这台平板已被电脑撤销，请在电脑上生成新的配对码后重新配对。") },
                confirmButton = { Button(onClick = { onUnpaired() }) { Text("重新配对") } },
            )
        }
        if (confirmUnpair) {
            AlertDialog(
                onDismissRequest = { confirmUnpair = false },
                title = { Text("重新配对？") },
                text = { Text("将断开与当前电脑的连接。之后需要在电脑上生成新的配对码。") },
                confirmButton = {
                    Button(onClick = {
                        confirmUnpair = false
                        TunnelService.stop(this@MainActivity)
                        Store(this@MainActivity).desktopId = null
                        onUnpaired()
                    }) { Text("断开并重新配对") }
                },
                dismissButton = { TextButton(onClick = { confirmUnpair = false }) { Text("取消") } },
            )
        }
    }

    private fun barColor(s: TunnelStatus): Color = when (s) {
        is TunnelStatus.Connected -> if (s.path == "direct") Color(0xFF2E7D32) else Color(0xFF1565C0)
        TunnelStatus.Connecting -> Color(0xFF616161)
        TunnelStatus.Revoked -> Color(0xFFC62828)
    }

    private fun download(url: String, disposition: String?, mime: String?) {
        val name = URLUtil.guessFileName(url, disposition, mime)
        val req = DownloadManager.Request(Uri.parse(url))
            .setTitle(name)
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
        if (mime != null) req.setMimeType(mime)
        getSystemService(DownloadManager::class.java).enqueue(req)
        Toast.makeText(this, "正在下载 $name", Toast.LENGTH_SHORT).show()
    }
}
