package com.airemote.channelsbridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import com.google.gson.Gson
import com.google.gson.JsonObject
import org.java_websocket.client.WebSocketClient
import org.java_websocket.handshake.ServerHandshake
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import java.util.Timer
import java.util.TimerTask

/**
 * Foreground service that maintains a WebSocket connection to the MCP server.
 *
 * The server sends JSON command messages like:
 *   {"id": "abc", "method": "GET", "path": "/api/status"}
 *   {"id": "def", "method": "POST", "path": "/api/resume", "body": null}
 *
 * This service proxies them to localhost:57000 (Channels app API) and returns:
 *   {"id": "abc", "status": 200, "body": {...}}
 *   {"id": "def", "status": 200, "body": {...}}
 */
class BridgeService : Service() {

    companion object {
        private const val TAG = "AIRemoteBridge"
        private const val CHANNEL_ID = "ai_remote_bridge_service"
        private const val NOTIFICATION_ID = 1
        private const val CHANNELS_API_BASE = "http://127.0.0.1:57000"
        private const val RECONNECT_DELAY_MS = 5000L
        private const val MAX_RECONNECT_DELAY_MS = 60000L
        private const val HEARTBEAT_INTERVAL_MS = 30000L

        /** Broadcast action for status updates to MainActivity */
        const val ACTION_STATUS = "com.airemote.channelsbridge.STATUS"
        const val EXTRA_STATE = "state"     // "connected", "disconnected", "connecting", "error"
        const val EXTRA_DETAIL = "detail"   // human-readable detail
    }

    private var wsClient: WebSocketClient? = null
    private var reconnectTimer: Timer? = null
    private var heartbeatTimer: Timer? = null
    private var currentReconnectDelay = RECONNECT_DELAY_MS
    private var wakeLock: PowerManager.WakeLock? = null
    private val gson = Gson()
    @Volatile private var running = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        acquireWakeLock()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, buildNotification("Connecting..."))
        running = true
        broadcastStatus("connecting", "Starting bridge service...")
        connectWebSocket()
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        broadcastStatus("disconnected", "Service stopped")
        heartbeatTimer?.cancel()
        reconnectTimer?.cancel()
        wsClient?.close()
        releaseWakeLock()
        super.onDestroy()
    }

    // -----------------------------------------------------------------
    // WebSocket connection
    // -----------------------------------------------------------------

    private fun connectWebSocket() {
        val host = BridgeConfig.getServerHost(this)
        val port = BridgeConfig.getServerPort(this)
        val token = BridgeConfig.getAuthToken(this)
        val deviceName = BridgeConfig.getDeviceName(this)

        if (host.isBlank()) {
            updateNotification("Not configured")
            broadcastStatus("error", "Server address not configured")
            return
        }

        val uri = URI("ws://$host:$port/bridge")
        if (BuildConfig.DEBUG) Log.d(TAG, "Connecting to $uri")
        broadcastStatus("connecting", "Connecting to $host:$port...")

        wsClient = object : WebSocketClient(uri) {
            override fun onOpen(handshake: ServerHandshake?) {
                if (BuildConfig.DEBUG) Log.d(TAG, "WebSocket connected")
                currentReconnectDelay = RECONNECT_DELAY_MS

                // Send registration message with auth token and device info
                val reg = JsonObject().apply {
                    addProperty("type", "register")
                    addProperty("token", token)
                    addProperty("device_name", deviceName)
                    addProperty("device_model", Build.MODEL)
                    addProperty("device_manufacturer", Build.MANUFACTURER)
                }
                send(reg.toString())
                updateNotification("Connected to $host")
                broadcastStatus("connected", "Connected to $host:$port")
                startHeartbeat()
            }

            override fun onMessage(message: String?) {
                if (message == null) return
                handleCommand(message)
            }

            override fun onClose(code: Int, reason: String?, remote: Boolean) {
                Log.w(TAG, "WebSocket closed: code=$code reason=$reason remote=$remote")
                stopHeartbeat()
                updateNotification("Disconnected — reconnecting...")
                broadcastStatus("disconnected", "Disconnected — reconnecting...")
                scheduleReconnect()
            }

            override fun onError(ex: Exception?) {
                Log.e(TAG, "WebSocket error", ex)
                broadcastStatus("error", ex?.message ?: "Connection error")
            }
        }.also {
            it.connectionLostTimeout = 60
            it.connect()
        }
    }

    // -----------------------------------------------------------------
    // Command handling — proxy to Channels localhost API
    // -----------------------------------------------------------------

    private fun handleCommand(raw: String) {
        Thread {
            try {
                val cmd = gson.fromJson(raw, JsonObject::class.java)
                val id = cmd.get("id")?.asString ?: ""
                val method = cmd.get("method")?.asString ?: "GET"
                val path = cmd.get("path")?.asString ?: "/api/status"
                val body = cmd.get("body")?.toString()

                val result = proxyToChannels(method, path, body)
                val response = JsonObject().apply {
                    addProperty("id", id)
                    addProperty("status", result.first)
                    add("body", gson.fromJson(result.second, JsonObject::class.java)
                        ?: com.google.gson.JsonNull.INSTANCE)
                }
                wsClient?.send(response.toString())
            } catch (e: Exception) {
                Log.e(TAG, "Error handling command", e)
                try {
                    val cmd = gson.fromJson(raw, JsonObject::class.java)
                    val id = cmd.get("id")?.asString ?: ""
                    val errResp = JsonObject().apply {
                        addProperty("id", id)
                        addProperty("status", 502)
                        add("body", JsonObject().apply {
                            addProperty("error", e.message ?: "Bridge proxy error")
                        })
                    }
                    wsClient?.send(errResp.toString())
                } catch (_: Exception) {}
            }
        }.start()
    }

    /**
     * Makes an HTTP request to the Channels app on localhost:57000.
     * Returns (statusCode, responseBody).
     */
    private fun proxyToChannels(method: String, path: String, body: String?): Pair<Int, String> {
        val url = URL("$CHANNELS_API_BASE$path")
        val conn = url.openConnection() as HttpURLConnection
        return try {
            conn.requestMethod = method.uppercase()
            conn.connectTimeout = 5000
            conn.readTimeout = 5000
            conn.setRequestProperty("Accept", "application/json")

            if (body != null && method.uppercase() == "POST") {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                OutputStreamWriter(conn.outputStream).use { it.write(body) }
            }

            val status = conn.responseCode
            val stream = if (status in 200..299) conn.inputStream else conn.errorStream
            val responseBody = stream?.let {
                BufferedReader(InputStreamReader(it)).use { r -> r.readText() }
            } ?: "{}"

            Pair(status, responseBody)
        } catch (e: java.net.ConnectException) {
            // Channels app not running or not listening
            Pair(503, """{"error":"Channels app not reachable on localhost:57000"}""")
        } catch (e: Exception) {
            Pair(502, """{"error":"${e.message?.replace("\"", "'")}"}""")
        } finally {
            conn.disconnect()
        }
    }

    // -----------------------------------------------------------------
    // Reconnection with exponential backoff
    // -----------------------------------------------------------------

    private fun scheduleReconnect() {
        if (!running) return
        reconnectTimer?.cancel()
        reconnectTimer = Timer().apply {
            schedule(object : TimerTask() {
                override fun run() {
                    if (running) connectWebSocket()
                }
            }, currentReconnectDelay)
        }
        // Exponential backoff, capped
        currentReconnectDelay = (currentReconnectDelay * 2).coerceAtMost(MAX_RECONNECT_DELAY_MS)
    }

    // -----------------------------------------------------------------
    // Heartbeat to keep connection alive
    // -----------------------------------------------------------------

    private fun startHeartbeat() {
        stopHeartbeat()
        heartbeatTimer = Timer().apply {
            scheduleAtFixedRate(object : TimerTask() {
                override fun run() {
                    try {
                        wsClient?.sendPing()
                    } catch (_: Exception) {}
                }
            }, HEARTBEAT_INTERVAL_MS, HEARTBEAT_INTERVAL_MS)
        }
    }

    private fun stopHeartbeat() {
        heartbeatTimer?.cancel()
        heartbeatTimer = null
    }

    // -----------------------------------------------------------------
    // Notification
    // -----------------------------------------------------------------

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "AI Remote Bridge",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "AI Remote Bridge service for Channels DVR playback"
            }
            getSystemService(NotificationManager::class.java)
                ?.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(text: String): Notification {
        val pi = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("AI Remote Bridge")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentIntent(pi)
                .setOngoing(true)
                .build()
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
                .setContentTitle("AI Remote Bridge")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentIntent(pi)
                .setOngoing(true)
                .build()
        }
    }

    private fun updateNotification(text: String) {
        try {
            getSystemService(NotificationManager::class.java)
                ?.notify(NOTIFICATION_ID, buildNotification(text))
        } catch (_: Exception) {}
    }

    private fun broadcastStatus(state: String, detail: String) {
        sendBroadcast(Intent(ACTION_STATUS).apply {
            putExtra(EXTRA_STATE, state)
            putExtra(EXTRA_DETAIL, detail)
            setPackage(packageName)
        })
    }

    // -----------------------------------------------------------------
    // Wake lock
    // -----------------------------------------------------------------

    private fun acquireWakeLock() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "AIRemoteBridge::BridgeWakeLock"
        ).apply { acquire(10 * 60 * 60 * 1000L) } // 10 hours max
    }

    private fun releaseWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }
}
