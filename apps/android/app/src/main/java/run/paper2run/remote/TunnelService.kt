package run.paper2run.remote

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * Foreground service owning the single `connect` process for this app's key.
 * Starting it again replaces any running process (one tunnel per key).
 */
class TunnelService : Service() {
    companion object {
        private const val CHANNEL = "tunnel"
        private const val NOTIFICATION_ID = 1
        private const val RESTART_DELAY_MS = 2000L
        private val _status = MutableStateFlow<TunnelStatus>(TunnelStatus.Connecting)
        val status: StateFlow<TunnelStatus> = _status
        private val _port = MutableStateFlow(0)
        val port: StateFlow<Int> = _port

        fun start(ctx: Context) {
            val i = Intent(ctx, TunnelService::class.java)
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i)
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, TunnelService::class.java))
        }
    }

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var process: Process? = null
    @Volatile private var stopping = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        goForeground()
        stopping = false
        // A second start (screen reopened, permission dialog) keeps the running tunnel.
        if (process?.isAlive != true) launch()
        return START_STICKY
    }

    private fun goForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "与电脑的连接", NotificationManager.IMPORTANCE_LOW))
        }
        val n = (if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, CHANNEL) else Notification.Builder(this))
            .setSmallIcon(R.drawable.ic_stat_logo)
            .setContentTitle("Paper2Run 远程")
            .setContentText("保持与电脑的连接")
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIFICATION_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, n)
        }
    }

    @Synchronized
    private fun launch() {
        process?.destroy()
        val store = Store(this)
        val desktop = store.desktopId ?: return stopSelf()
        val port = try { Tunnel.pickPort(store.port) } catch (e: Exception) {
            Log.e("p2r", "no free port", e); return stopSelf()
        }
        store.port = port
        _port.value = port
        _status.value = TunnelStatus.Connecting
        val proc = try { Tunnel.connect(this, desktop, port) } catch (e: Exception) {
            Log.e("p2r", "cannot start tunnel", e); scheduleRestart(); return
        }
        process = proc
        Thread({ ProcessLines.drain(proc.errorStream) { Log.i("p2r-tunnel", it) } }, "tunnel-stderr").start()
        Thread({
            ProcessLines.drain(proc.inputStream) { line -> TunnelStatus.parse(line)?.let { _status.value = it } }
            val exit = proc.waitFor()
            onExit(proc, exit)
        }, "tunnel-stdout").start()
    }

    private fun onExit(proc: Process, exit: Int) {
        if (proc !== process) return
        Log.w("p2r", "tunnel exited with $exit")
        if (exit == Tunnel.EXIT_REVOKED) {
            Store(this).desktopId = null
            _status.value = TunnelStatus.Revoked
            main.post { stopSelf() }
        } else if (!stopping) {
            scheduleRestart()
        }
    }

    private fun scheduleRestart() {
        _status.value = TunnelStatus.Connecting
        main.postDelayed({ if (!stopping) launch() }, RESTART_DELAY_MS)
    }

    override fun onDestroy() {
        stopping = true
        main.removeCallbacksAndMessages(null)
        process?.destroy()
        process = null
        super.onDestroy()
    }
}
