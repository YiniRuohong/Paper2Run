package run.paper2run.remote

import android.Manifest
import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.view.ViewGroup
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
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
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
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
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
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
    private val bridgeJs by lazy { assets.open("bridge.js").bufferedReader().use { it.readText() } }

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        val store = Store(this)
        setContent {
            P2RTheme {
                val p = LocalPalette.current
                Surface(Modifier.fillMaxSize(), color = p.side) {
                    Box(Modifier.fillMaxSize().safeDrawingPadding()) {
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
    }

    @Composable
    private fun LogoTile(size: Int) {
        Image(painterResource(R.drawable.logo_tile), contentDescription = "Paper2Run", modifier = Modifier.size(size.dp))
    }

    @Composable
    private fun PairScreen(store: Store, onPaired: () -> Unit) {
        val p = LocalPalette.current
        var code by remember { mutableStateOf("") }
        var name by remember { mutableStateOf(store.deviceName) }
        var busy by remember { mutableStateOf(false) }
        var error by remember { mutableStateOf<String?>(null) }
        val scope = rememberCoroutineScope()
        val normalized = PairCode.normalize(code)
        Box(Modifier.fillMaxSize().background(p.bg), contentAlignment = Alignment.Center) {
            Column(
                Modifier.widthIn(max = 480.dp).padding(24.dp)
                    .clip(RoundedCornerShape(16.dp)).background(p.surface)
                    .border(1.dp, p.line, RoundedCornerShape(16.dp)).padding(28.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp),
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    LogoTile(56)
                    Spacer(Modifier.width(14.dp))
                    Column {
                        Text("Paper2Run", fontSize = 13.sp, color = p.muted)
                        Text("连接到电脑", fontSize = 22.sp, fontWeight = FontWeight.SemiBold, color = p.text)
                    }
                }
                Text(
                    "在电脑上打开 Paper2Run → 设置 → 平板远程 → 生成配对码，然后在这里输入。" +
                        "同一局域网时自动直连，不在同一网络时经中继服务器连接，内容端到端加密。",
                    color = p.muted, fontSize = 14.sp, lineHeight = 21.sp,
                )
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
                error?.let { Text(it, color = p.bad, fontSize = 14.sp) }
                Button(
                    enabled = !busy && normalized != null && name.isNotBlank(),
                    modifier = Modifier.fillMaxWidth().height(48.dp),
                    shape = RoundedCornerShape(10.dp),
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
                ) { Text(if (busy) "正在配对…" else "配对", fontSize = 16.sp) }
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled", "JavascriptInterface")
    @Composable
    private fun MainScreen(onUnpaired: () -> Unit) {
        val p = LocalPalette.current
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
        var loadedPort by remember { mutableIntStateOf(0) }
        var confirmUnpair by remember { mutableStateOf(false) }
        var canGoBack by remember { mutableStateOf(false) }

        // The web UI is a hash-routed SPA served at "/" by the desktop backend.
        // Reload only when the local tunnel port changes (tunnel restarted elsewhere).
        LaunchedEffect(status, port, web) {
            val w = web ?: return@LaunchedEffect
            if (status is TunnelStatus.Connected && port != 0 && port != loadedPort) {
                w.visibility = android.view.View.VISIBLE
                w.loadUrl("http://127.0.0.1:$port/")
                loadedPort = port
            }
        }
        BackHandler(enabled = canGoBack) { web?.goBack() }

        Column(Modifier.fillMaxSize()) {
            TopBar(status, onReload = { web?.reload() }, onUnpair = { confirmUnpair = true })
            Box(Modifier.weight(1f).fillMaxWidth().background(p.bg)) {
                AndroidView(modifier = Modifier.fillMaxSize(), factory = { ctx ->
                    WebView(ctx).apply {
                        // Without explicit params the WebView measures to its content, and
                        // the page (height: 100dvh) then sees a 0-px viewport and draws nothing.
                        layoutParams = ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
                        setBackgroundColor(p.bg.toArgb())
                        // Hidden until the first load: an empty WebView paints its background over
                        // the whole window, hiding the status bar above it.
                        visibility = android.view.View.INVISIBLE
                        settings.javaScriptEnabled = true
                        settings.domStorageEnabled = true
                        // The layout is sized in CSS px; system font scaling would overflow it.
                        settings.textZoom = 100
                        addJavascriptInterface(WebBridge(this@MainActivity, this), "P2RAndroid")
                        webViewClient = object : WebViewClient() {
                            override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
                                view.evaluateJavascript(bridgeJs, null)
                            }
                            override fun onPageFinished(view: WebView, url: String?) {
                                view.evaluateJavascript(bridgeJs, null)
                            }
                            override fun doUpdateVisitedHistory(view: WebView, url: String?, isReload: Boolean) {
                                canGoBack = view.canGoBack()
                            }
                            // Only the tunnel's loopback origin renders here (it can reach the
                            // bridge); every other link opens in the system browser.
                            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                                val uri = request.url
                                if (uri.host == "127.0.0.1" && uri.scheme == "http") return false
                                runCatching { startActivity(Intent(Intent.ACTION_VIEW, uri)) }
                                return true
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
                if (loadedPort == 0) Connecting(status)
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

    /** Slim bar in the web sidebar's colour: honest connection path and actions (the page shows the brand). */
    @Composable
    private fun TopBar(status: TunnelStatus, onReload: () -> Unit, onUnpair: () -> Unit) {
        val p = LocalPalette.current
        val dot = when (status) {
            is TunnelStatus.Connected -> if (status.path == "direct") p.ok else p.accent
            TunnelStatus.Connecting -> p.faint
            TunnelStatus.Revoked -> p.bad
        }
        Column(Modifier.fillMaxWidth().background(p.side)) {
            Row(
                Modifier.fillMaxWidth().height(40.dp).padding(start = 20.dp, end = 8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Box(Modifier.size(8.dp).clip(CircleShape).background(dot))
                Spacer(Modifier.width(6.dp))
                Text(status.label, color = p.muted, fontSize = 13.sp)
                Spacer(Modifier.weight(1f))
                TextButton(onClick = onReload) { Text("刷新", color = p.muted, fontSize = 13.sp) }
                TextButton(onClick = onUnpair) { Text("重新配对", color = p.accent, fontSize = 13.sp) }
            }
            Box(Modifier.fillMaxWidth().height(1.dp).background(p.line))
        }
    }

    @Composable
    private fun Connecting(status: TunnelStatus) {
        val p = LocalPalette.current
        Box(Modifier.fillMaxSize().background(p.bg), contentAlignment = Alignment.Center) {
            Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(14.dp)) {
                LogoTile(88)
                Text(if (status is TunnelStatus.Connected) "正在打开页面…" else "正在连接电脑…", color = p.text, fontSize = 17.sp)
                Text("同一局域网时自动直连；不在同一网络时经中继连接", color = p.muted, fontSize = 13.sp)
            }
        }
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
