package run.paper2run.remote

import android.content.Context
import java.io.File
import java.net.InetAddress
import java.net.ServerSocket

/**
 * The p2r-tunnel binary ships as libp2rtunnel.so (extracted to nativeLibraryDir,
 * which Android lets apps execute). Relay address, rendezvous id and the relay's
 * CA come from assets copied out of apps/tunnel/deploy at build time.
 */
object Tunnel {
    const val FIRST_PORT = 18080
    const val LAST_PORT = 18089
    const val EXIT_REVOKED = 5

    data class Config(val relayUrl: String, val relayQuicPort: Int, val rendezvousId: String)

    fun binary(ctx: Context): File = File(ctx.applicationInfo.nativeLibraryDir, "libp2rtunnel.so")
    fun key(ctx: Context): File = File(ctx.filesDir, "key")

    fun config(ctx: Context): Config {
        val text = ctx.assets.open("remote.json").bufferedReader().use { it.readText() }
        fun field(name: String) = Regex("\"$name\"\\s*:\\s*\"?([^\",}]+)").find(text)?.groupValues?.get(1)?.trim()
            ?: error("remote.json missing $name")
        return Config(field("relay_url"), field("relay_quic_port").toInt(), field("rendezvous_id"))
    }

    private fun caFile(ctx: Context): File {
        val out = File(ctx.filesDir, "relay-ca.pem")
        ctx.assets.open("relay-ca.pem").use { src -> out.outputStream().use { src.copyTo(it) } }
        return out
    }

    private fun netArgs(ctx: Context): List<String> {
        val c = config(ctx)
        return listOf("--key", key(ctx).path, "--relay", c.relayUrl,
            "--relay-quic-port", c.relayQuicPort.toString(), "--relay-ca", caFile(ctx).path)
    }

    data class Run(val exitCode: Int, val stderr: String)

    /** Blocking: run `pair` and return its exit code and stderr. */
    fun pair(ctx: Context, code: String, name: String): Run {
        val out = File(ctx.cacheDir, "desktop.id").apply { delete() }
        val cmd = listOf(binary(ctx).path, "pair") + netArgs(ctx) +
            listOf("--rendezvous", config(ctx).rendezvousId, "--code", code, "--name", name, "--out", out.path)
        val proc = ProcessBuilder(cmd).directory(ctx.filesDir).start()
        proc.outputStream.close()
        val stderr = StringBuilder()
        val errReader = Thread { stderr.append(proc.errorStream.bufferedReader().readText()) }.apply { start() }
        proc.inputStream.bufferedReader().readText()
        val exit = proc.waitFor()
        errReader.join()
        if (exit == 0) Store(ctx).desktopId = out.readText().trim()
        return Run(exit, stderr.toString())
    }

    /** Start `connect --status-json`; caller owns the process. */
    fun connect(ctx: Context, desktopId: String, port: Int): Process {
        val cmd = listOf(binary(ctx).path, "connect") + netArgs(ctx) +
            listOf("--peer", desktopId, "--listen", "127.0.0.1:$port", "--status-json")
        return ProcessBuilder(cmd).directory(ctx.filesDir).redirectErrorStream(false).start()
    }

    /** First free loopback port in 18080..18089, preferring the last one used. */
    fun pickPort(preferred: Int): Int {
        val loopback = InetAddress.getByName("127.0.0.1")
        val candidates = (listOf(preferred) + (FIRST_PORT..LAST_PORT)).filter { it in FIRST_PORT..LAST_PORT }.distinct()
        for (p in candidates) {
            try { ServerSocket(p, 1, loopback).close(); return p } catch (_: Exception) { }
        }
        error("ports $FIRST_PORT-$LAST_PORT are all busy")
    }
}
