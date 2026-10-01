package com.airemote.channelsbridge

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import android.view.View
import androidx.appcompat.app.AppCompatActivity

/**
 * Configuration screen with live connection status.
 * Status bar at top shows colored dot + state + detail text.
 * Form fields scroll under the keyboard thanks to adjustResize.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var hostEdit: EditText
    private lateinit var portEdit: EditText
    private lateinit var tokenEdit: EditText
    private lateinit var nameEdit: EditText
    private lateinit var enableSwitch: Switch
    private lateinit var statusText: TextView
    private lateinit var statusDetail: TextView
    private lateinit var statusDot: View
    private lateinit var statusCard: View

    private var nsdManager: NsdManager? = null
    private var discoveryListener: NsdManager.DiscoveryListener? = null
    @Volatile private var resolving = false
    @Volatile private var autoFillRequested = false

    companion object {
        /** mDNS service type advertised by the server (keep in sync with BridgeManager). */
        private const val NSD_SERVICE_TYPE = "_airemote-bridge._tcp."
    }

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            val state = intent?.getStringExtra(BridgeService.EXTRA_STATE) ?: return
            val detail = intent.getStringExtra(BridgeService.EXTRA_DETAIL) ?: ""
            runOnUiThread { showStatus(state, detail) }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        hostEdit = findViewById(R.id.editHost)
        portEdit = findViewById(R.id.editPort)
        tokenEdit = findViewById(R.id.editToken)
        nameEdit = findViewById(R.id.editDeviceName)
        enableSwitch = findViewById(R.id.switchEnable)
        statusText = findViewById(R.id.textStatus)
        statusDetail = findViewById(R.id.textStatusDetail)
        statusDot = findViewById(R.id.statusDot)
        statusCard = findViewById(R.id.statusCard)

        // Load saved config
        hostEdit.setText(BridgeConfig.getServerHost(this))
        portEdit.setText(BridgeConfig.getServerPort(this).toString())
        tokenEdit.setText(BridgeConfig.getAuthToken(this))
        nameEdit.setText(BridgeConfig.getDeviceName(this))
        enableSwitch.isChecked = BridgeConfig.isEnabled(this)

        nsdManager = getSystemService(Context.NSD_SERVICE) as? NsdManager

        updateLocalStatus()

        findViewById<Button>(R.id.btnSave).setOnClickListener {
            saveAndApply()
        }

        findViewById<Button>(R.id.btnDiscover).setOnClickListener {
            autoFillRequested = true
            Toast.makeText(this, "Searching the network for your server…", Toast.LENGTH_SHORT).show()
            restartDiscovery()
        }

        enableSwitch.setOnCheckedChangeListener { _, isChecked ->
            BridgeConfig.setEnabled(this, isChecked)
            if (isChecked) {
                saveConfig()
                startBridgeService()
            } else {
                stopBridgeService()
                showStatus("disconnected", "Bridge disabled")
            }
        }
    }

    override fun onResume() {
        super.onResume()
        val filter = IntentFilter(BridgeService.ACTION_STATUS)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(statusReceiver, filter, RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(statusReceiver, filter)
        }
        // Auto-discover whenever the host is still blank so first-run users get
        // the address filled in without typing. An explicit tap forces a rescan.
        if (BridgeConfig.getServerHost(this).isBlank()) {
            autoFillRequested = true
            startDiscovery()
        }
    }

    override fun onPause() {
        super.onPause()
        try { unregisterReceiver(statusReceiver) } catch (_: Exception) {}
        stopDiscovery()
    }

    // -----------------------------------------------------------------
    // mDNS / NSD auto-discovery of the server
    // -----------------------------------------------------------------

    private fun restartDiscovery() {
        stopDiscovery()
        startDiscovery()
    }

    private fun startDiscovery() {
        val mgr = nsdManager ?: return
        if (discoveryListener != null) return
        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {}
            override fun onServiceFound(info: NsdServiceInfo) {
                if (info.serviceType.trimEnd('.').endsWith("_airemote-bridge._tcp")) {
                    resolveService(info)
                }
            }
            override fun onServiceLost(info: NsdServiceInfo) {}
            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                try { mgr.stopServiceDiscovery(this) } catch (_: Exception) {}
            }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
        }
        discoveryListener = listener
        try {
            mgr.discoverServices(NSD_SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (_: Exception) {
            discoveryListener = null
        }
    }

    private fun stopDiscovery() {
        val mgr = nsdManager ?: return
        val listener = discoveryListener ?: return
        discoveryListener = null
        try { mgr.stopServiceDiscovery(listener) } catch (_: Exception) {}
    }

    private fun resolveService(info: NsdServiceInfo) {
        val mgr = nsdManager ?: return
        if (resolving) return
        resolving = true
        mgr.resolveService(info, object : NsdManager.ResolveListener {
            override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                resolving = false
            }
            override fun onServiceResolved(serviceInfo: NsdServiceInfo) {
                resolving = false
                // Prefer the authoritative LAN IP the server advertises in its
                // TXT "addr" record; fall back to the resolved host otherwise
                // (the raw host may be a docker/veth address on multi-NIC hosts).
                val txtAddr = try {
                    serviceInfo.attributes?.get("addr")?.toString(Charsets.UTF_8)?.trim()
                } catch (_: Exception) { null }
                val host = txtAddr?.takeIf { it.isNotBlank() }
                    ?: serviceInfo.host?.hostAddress
                    ?: return
                val port = serviceInfo.port
                runOnUiThread { onServerDiscovered(host, port) }
            }
        })
    }

    /** Fill host/port from a discovered server (only when the user hasn't set one). */
    private fun onServerDiscovered(host: String, port: Int) {
        val currentHost = hostEdit.text.toString().trim()
        val shouldFill = autoFillRequested || currentHost.isBlank()
        if (shouldFill && currentHost != host) {
            hostEdit.setText(host)
            portEdit.setText(port.toString())
            saveConfig()
            autoFillRequested = false
            Toast.makeText(this, "Found server at $host:$port", Toast.LENGTH_SHORT).show()
            if (enableSwitch.isChecked) {
                saveAndApply()
            } else {
                showStatus("disconnected", "Found server at $host:$port — enable to connect")
            }
        } else {
            statusDetail.text = "Server available at $host:$port"
        }
        stopDiscovery()
    }

    private fun saveConfig() {
        BridgeConfig.setServerHost(this, hostEdit.text.toString().trim())
        BridgeConfig.setServerPort(this, portEdit.text.toString().trim().toIntOrNull() ?: 8771)
        BridgeConfig.setAuthToken(this, tokenEdit.text.toString().trim())
        BridgeConfig.setDeviceName(this, nameEdit.text.toString().trim().ifBlank { Build.MODEL })
    }

    private fun saveAndApply() {
        saveConfig()
        if (enableSwitch.isChecked) {
            stopBridgeService()
            startBridgeService()
            showStatus("connecting", "Restarting with new configuration...")
        } else {
            showStatus("disconnected", "Configuration saved — enable the switch to connect")
        }
    }

    private fun startBridgeService() {
        val intent = Intent(this, BridgeService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
    }

    private fun stopBridgeService() {
        stopService(Intent(this, BridgeService::class.java))
    }

    /** Set status from local state (no broadcast yet received). */
    private fun updateLocalStatus() {
        val host = BridgeConfig.getServerHost(this)
        val enabled = BridgeConfig.isEnabled(this)
        when {
            host.isBlank() -> showStatus("connecting", "Searching the network for your server…")
            enabled -> showStatus("connecting", "Service should be running — waiting for status...")
            else -> showStatus("disconnected", "Bridge disabled")
        }
    }

    /** Update the status bar UI. */
    private fun showStatus(state: String, detail: String) {
        val (label, dotColor, cardColor) = when (state) {
            "connected" -> Triple("Connected", Color.parseColor("#4CAF50"), Color.parseColor("#E8F5E9"))
            "connecting" -> Triple("Connecting…", Color.parseColor("#FF9800"), Color.parseColor("#FFF3E0"))
            "error" -> Triple("Error", Color.parseColor("#F44336"), Color.parseColor("#FFEBEE"))
            else -> Triple("Disconnected", Color.parseColor("#9E9E9E"), Color.parseColor("#F5F5F5"))
        }
        statusText.text = label
        statusDetail.text = detail

        // Round dot
        val dot = GradientDrawable()
        dot.shape = GradientDrawable.OVAL
        dot.setColor(dotColor)
        statusDot.background = dot

        statusCard.setBackgroundColor(cardColor)
    }
}
