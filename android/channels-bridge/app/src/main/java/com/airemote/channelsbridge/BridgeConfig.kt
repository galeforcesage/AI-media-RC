package com.airemote.channelsbridge

import android.content.Context
import android.content.SharedPreferences

/**
 * Stores bridge configuration: server host, port, and auth token.
 */
object BridgeConfig {
    private const val PREFS_NAME = "ai_remote_bridge_prefs"
    private const val KEY_SERVER_HOST = "server_host"
    private const val KEY_SERVER_PORT = "server_port"
    private const val KEY_AUTH_TOKEN = "auth_token"
    private const val KEY_ENABLED = "enabled"
    private const val KEY_DEVICE_NAME = "device_name"

    private fun prefs(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun getServerHost(ctx: Context): String =
        prefs(ctx).getString(KEY_SERVER_HOST, "") ?: ""

    fun setServerHost(ctx: Context, host: String) =
        prefs(ctx).edit().putString(KEY_SERVER_HOST, host).apply()

    fun getServerPort(ctx: Context): Int =
        prefs(ctx).getInt(KEY_SERVER_PORT, 8771)

    fun setServerPort(ctx: Context, port: Int) =
        prefs(ctx).edit().putInt(KEY_SERVER_PORT, port).apply()

    fun getAuthToken(ctx: Context): String =
        prefs(ctx).getString(KEY_AUTH_TOKEN, "") ?: ""

    fun setAuthToken(ctx: Context, token: String) =
        prefs(ctx).edit().putString(KEY_AUTH_TOKEN, token).apply()

    fun isEnabled(ctx: Context): Boolean =
        prefs(ctx).getBoolean(KEY_ENABLED, false)

    fun setEnabled(ctx: Context, enabled: Boolean) =
        prefs(ctx).edit().putBoolean(KEY_ENABLED, enabled).apply()

    fun getDeviceName(ctx: Context): String =
        prefs(ctx).getString(KEY_DEVICE_NAME, android.os.Build.MODEL) ?: android.os.Build.MODEL

    fun setDeviceName(ctx: Context, name: String) =
        prefs(ctx).edit().putString(KEY_DEVICE_NAME, name).apply()
}
